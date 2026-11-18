#!/usr/bin/env python3

"""Executes arbitrary scripts passed in via TCP socket."""

import abc
import argparse
from collections.abc import Iterator, Sequence
import datetime
import logging
import os
import queue
import selectors
import shutil
import socket
import subprocess
import sys
import threading
import time
import types
from typing import Any, Callable, Optional, cast
import urllib.parse


sel = selectors.DefaultSelector()

CMD = os.path.splitext(os.path.basename(sys.argv[0]))[0]

NORMAL = 'normal'
LOW = 'low'
CRITICAL = 'critical'


class Notifier(abc.ABC):
  """Shared interface for downstream notification methods."""

  name: str = ''
  binary_name: Optional[str] = None
  fallback_binary: Optional[str] = None

  def __init__(self, args: argparse.Namespace) -> None:
    self.args = args
    self.binary = self._resolve_binary()

  def _resolve_binary(self) -> Optional[str]:
    if not self.binary_name and not self.fallback_binary:
      return None
    if self.binary_name:
      found = shutil.which(self.binary_name)
      if found:
        return found
    if (
        self.fallback_binary
        and os.path.isfile(self.fallback_binary)
        and os.access(self.fallback_binary, os.X_OK)
    ):
      return self.fallback_binary
    return None

  def _spawn(self, cmd: Sequence[str]) -> None:
    try:
      subprocess.Popen(
          cmd,
          stdin=subprocess.DEVNULL,
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
      )
    except OSError as e:
      logging.debug('Failed to execute %s: %s', self.name, e)

  @abc.abstractmethod
  def is_available(self) -> bool:
    """Returns True if this notification method can be used."""

  @abc.abstractmethod
  def send(
      self,
      message: str,
      *,
      urgency: str = NORMAL,
      category: str = '',
      expire: int = 2000,
      now: Optional[datetime.datetime] = None,
  ) -> None:
    """Sends a notification."""


class TmuxNotifier(Notifier):
  """Sends critical notifications via tmux display-message."""

  name = 'tmux'
  binary_name = 'tmux'
  fallback_binary = '/usr/bin/tmux'

  def is_available(self) -> bool:
    return bool(os.getenv('TMUX')) and self.binary is not None

  def send(
      self,
      message: str,
      *,
      urgency: str = NORMAL,
      category: str = '',
      expire: int = 2000,
      now: Optional[datetime.datetime] = None,
  ) -> None:
    del category, expire, now
    if urgency not in {CRITICAL} or not self.binary:
      return
    self._spawn([self.binary, 'display-message', ' ' + message])


class XtermNotifier(Notifier):
  """Updates the xterm window title via OSC escape sequence."""

  name = 'xterm'

  def is_available(self) -> bool:
    return not os.getenv('TMUX') and os.getenv('TERM', '').startswith('xterm')

  def send(
      self,
      message: str,
      *,
      urgency: str = NORMAL,
      category: str = '',
      expire: int = 2000,
      now: Optional[datetime.datetime] = None,
  ) -> None:
    del urgency, category, expire
    ts = (now or datetime.datetime.now()).strftime('%H:%M:%S')
    sys.stdout.write(f'\x1B]0;[{ts}] {CMD}: {message}\x07\n')
    sys.stdout.flush()


class NotifySendNotifier(Notifier):
  """Sends desktop notifications via /usr/bin/notify-send."""

  name = 'notify-send'
  binary_name = 'notify-send'
  fallback_binary = '/usr/bin/notify-send'

  def is_available(self) -> bool:
    return (
        not getattr(self.args, 'headless', False) and self.binary is not None
    )

  def send(
      self,
      message: str,
      *,
      urgency: str = NORMAL,
      category: str = '',
      expire: int = 2000,
      now: Optional[datetime.datetime] = None,
  ) -> None:
    del now
    if urgency not in {NORMAL, CRITICAL} or not self.binary:
      return
    cmd = [
        self.binary,
        '-u',
        urgency,
        '-t',
        str(expire),
    ]
    if category:
      cmd.extend(['-c', category])
    cmd.append(f'{CMD}: {message}')
    self._spawn(cmd)


class NotifySelfNotifier(Notifier):
  """Sends G3N notifications via notify_self."""

  name = 'notify-self'
  binary_name = 'notify_self'
  fallback_binary = '/google/bin/releases/g3n-team/notify_self'

  def __init__(self, args: argparse.Namespace) -> None:
    super().__init__(args)
    host = socket.gethostname().split('.')[0]
    session = os.getenv('TMUX_SESSION')
    self.summary = (
        f'{CMD} ({session}) on {host}' if session else f'{CMD} on {host}'
    )
    target = session or args.socket
    self.uri = (
        'http://go/g3n-self?'
        f'host={urllib.parse.quote(host, safe="")}'
        f'&session={urllib.parse.quote(target, safe="")}'
    )

  def is_available(self) -> bool:
    return self.binary is not None

  def send(
      self,
      message: str,
      *,
      urgency: str = NORMAL,
      category: str = '',
      expire: int = 2000,
      now: Optional[datetime.datetime] = None,
  ) -> None:
    del expire, now
    if urgency not in {NORMAL, CRITICAL} or not self.binary:
      return
    if urgency == CRITICAL or category == 'failed':
      severity = 'red'
    elif message.startswith('KILLED:'):
      severity = 'yellow'
    else:
      severity = 'green'

    self._spawn([
        self.binary,
        f'--summary={self.summary}',
        f'--message={message}',
        f'--uri={self.uri}',
        f'--severity={severity}',
    ])


NOTIFIERS: dict[str, type[Notifier]] = {
    TmuxNotifier.name: TmuxNotifier,
    XtermNotifier.name: XtermNotifier,
    NotifySendNotifier.name: NotifySendNotifier,
    NotifySelfNotifier.name: NotifySelfNotifier,
}
DEFAULT_NOTIFICATIONS = ','.join(NOTIFIERS.keys())


def parse_notification_names(raw: str) -> list[str]:
  """Parses a comma-separated list of notification method names."""
  names: list[str] = []
  for part in raw.split(','):
    name = part.strip().lower().replace('_', '-')
    if not name or name == 'none':
      continue
    names.append(name)
  return names


def init_notifications(args: argparse.Namespace) -> list[Notifier]:
  """Instantiates available notification backends configured in args."""
  notifications: list[Notifier] = []
  seen: set[str] = set()
  for name in parse_notification_names(args.notifications):
    if name in seen:
      continue
    seen.add(name)
    notifier = NOTIFIERS[name](args)
    if notifier.is_available():
      logging.debug('Enabled notification method: %s', name)
      notifications.append(notifier)
    else:
      logging.debug('Notification method unavailable: %s', name)
  return notifications


def define_flags() -> argparse.Namespace:
  """Defines the flags for this program."""
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v',
      '--verbosity',
      action='store',
      default=logging.INFO,
      type=int,
      help='the logging verbosity',
      metavar='LEVEL',
  )
  parser.add_argument(
      '--max-interrupts',
      action='store',
      default=3,
      type=int,
      help='maximum number of interrupts before exiting',
      metavar='COUNT',
  )
  parser.add_argument(
      '-x',
      '--headless',
      action='store_true',
      default=False,
      help="if the machine is headless then notify-send isn't used",
  )
  parser.add_argument(
      '-n',
      '--notifications',
      action='store',
      default=DEFAULT_NOTIFICATIONS,
      type=str,
      help=(
          'comma-separated list of downstream notification methods '
          f'(choices: {", ".join(NOTIFIERS.keys())}, none; '
          'default: %(default)s)'
      ),
      metavar='METHODS',
  )
  parser.add_argument(
      '--polling-interval',
      action='store',
      default=0.1,
      type=float,
      help='number of seconds (float) between health checks',
      metavar='SECONDS',
  )
  parser.add_argument(
      '--reset-and-clear',
      action='store_true',
      default=False,
      help='reset and clear after keyboard interrupt',
  )
  parser.add_argument(
      '-V',
      '--version',
      action='version',
      version='%(prog)s version 0.1',
  )
  parser.add_argument(
      '--socket',
      nargs='?',
      default=os.getenv('EFIFO_SOCKET', os.getenv('EFIFO')),
      type=str,
      help='socket file',
      metavar='SOCKET',
  )

  args = parser.parse_args()
  check_flags(parser, args)
  return args


def check_flags(
    parser: argparse.ArgumentParser, args: argparse.Namespace
) -> None:
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not args.socket:
    parser.error('--socket file required')
  unknown = [
      name
      for name in parse_notification_names(args.notifications)
      if name not in NOTIFIERS
  ]
  if unknown:
    parser.error(
        f'unknown notification method(s): {", ".join(unknown)} '
        f'(choose from {", ".join(NOTIFIERS.keys())}, none)'
    )


def send_notification(
    notifications: Sequence[Notifier],
    msg: str,
    *format_args: Any,
    urgency: str = NORMAL,
    category: str = '',
    expire: int = 2000,
) -> None:
  """Send notifications across all active notification methods.

  Args:
    notifications: Enabled Notifier instances.
    msg: The message to show.
    *format_args: Extra string formatting args for msg.
    urgency: One of NORMAL, LOW or CRITICAL.
    category: A category for this notification.
    expire: Number of milliseconds to expire.
  """
  now = datetime.datetime.now()
  message = msg % format_args if format_args else msg
  for n in notifications:
    n.send(
        message,
        urgency=urgency,
        category=category,
        expire=expire,
        now=now,
    )


def rename_tab(msg: str, *args: Any) -> None:
  """Renames the tab executing this program.

  Args:
    msg: The message to show.
    *args: Extra string formatting files for msg.
  """
  if not os.getenv('TMUX'):
    return
  subprocess.call(
      ['tmux', 'rename-window', '-t', os.getenv('TMUX_PANE', ''), msg % args]
  )


IGNORED_COMMANDS = {'cd'}


def split_commands(s: str) -> Iterator[str]:
  for cmds in (t.split(';') for t in s.splitlines()):
    for cmd in cmds:
      yield cmd


def first_command(s: str) -> str:
  for cmd in split_commands(s):
    sp = cmd.split()
    if not sp or sp[0] in IGNORED_COMMANDS:
      continue
    return sp[0]
  return ''


def display_commands(s: str) -> str:
  ret = []
  for cmd in split_commands(s):
    sp = cmd.split()
    if not sp or sp[0] in IGNORED_COMMANDS:
      continue
    ret.append(cmd.strip())
  if not ret:
    return '<empty>'
  return '; '.join(ret)


executions = 0
last_script: Optional[str] = None

def execute_bash(
    args: argparse.Namespace,
    notifications: Sequence[Notifier],
    script: str,
    interrupt: Optional[threading.Event] = None,
) -> Optional[int]:
  """Execute this bash script.

  Args:
    args: Main program args.
    notifications: Enabled Notifier instances.
    script: Script to execute through `bash -x`.
    interrupt: When set, kill any running process.

  Returns:
    Status code.
  """
  global executions
  executions += 1
  display = display_commands(script)
  send_notification(
      notifications, 'Running: %s [%d]', display, executions, urgency=LOW
  )
  cmd = os.path.basename(first_command(script))
  rename_tab('%s..' % cmd)
  start = time.time()
  p = subprocess.Popen(['bash', '-x'], stdin=subprocess.PIPE, text=True)
  proc = types.SimpleNamespace(killed=False)

  def poll() -> None:
    while p.poll() is None:
      if interrupt and interrupt.is_set():
        logging.warning(f'Killing process {p}..')
        p.terminate()
        proc.killed = True
        return
      send_notification(
          notifications,
          'Running: %s %ds [%d]',
          display,
          time.time() - start,
          executions,
          urgency=LOW,
      )
      time.sleep(args.polling_interval)

  t = threading.Thread(target=poll)
  t.start()

  p.communicate(input=script)
  elapsed = time.time() - start
  t.join()

  if proc.killed:
    rename_tab(cmd)
    send_notification(
        notifications,
        'KILLED: %s %0.2fs',
        display,
        elapsed,
        category='done',
        expire=15000,
        urgency=NORMAL,
    )
    return None

  if p.returncode == 0:
    rename_tab(cmd)
    send_notification(
        notifications,
        'DONE: %s [%d] %0.2fs',
        display,
        p.returncode,
        elapsed,
        category='done',
        expire=15000,
        urgency=NORMAL,
    )
  else:
    rename_tab('%s!' % cmd)
    send_notification(
        notifications,
        'FAILED: %s [%d] %0.2fs',
        display,
        p.returncode,
        elapsed,
        category='failed',
        urgency=CRITICAL,
        expire=60000,
    )

  return p.returncode


def accept(sock: socket.socket) -> None:
  """Accepts the socket connection."""
  conn, addr = sock.accept()
  # If this is a socket file the addr is going to be empty. Let's use the
  # filename instead to have something to print.
  if not addr:
    addr = conn.getsockname()
  logging.debug(f'Accepted connection on {addr}')
  conn.setblocking(False)
  data = types.SimpleNamespace(addr=addr, read=bytes(), write=bytes())
  sel.register(conn, selectors.EVENT_READ, data=data)


def serve(
    key: selectors.SelectorKey, mask: int, scripts: queue.Queue[str]
) -> None:
  """Serves the connection and adds to scripts Queue.

  Args:
    key: The selector key.
    mask: The selector mask.
    scripts: The scripts queue.
  """
  conn = cast(socket.socket, key.fileobj)
  data = key.data
  if mask & selectors.EVENT_READ:
    buf = conn.recv(2**12)
    if buf:
      data.read += buf
    else:
      logging.debug(f'Closing connection to {data.addr}')
      sel.unregister(conn)
      conn.close()
      # Scripts contains the various scripts to be executed.
      scripts.put(data.read.decode())
  if mask & selectors.EVENT_WRITE:
    raise NotImplementedError('EVENT_WRITE is not written')


def dequeue(
    args: argparse.Namespace,
    notifications: Sequence[Notifier],
    scripts: queue.Queue[str],
    interrupt: threading.Event,
    shutdown: threading.Event,
    callback: Callable[[Optional[int]], None],
) -> None:
  """Dequeues events from the Queue and executes bash.

  Args:
    args: Main program args.
    notifications: Enabled Notifier instances.
    scripts: The scripts queue.
    interrupt: When set, kill any running process.
    shutdown: When set, shutdown this thread.
    callback: Callback function that takes the command status code as input.
  """
  global last_script
  while not shutdown.is_set():
    try:
      script = scripts.get(timeout=args.polling_interval)
      last_script = script
      retcode = execute_bash(
          args, notifications, script, interrupt=interrupt
      )
      callback(retcode)
    except queue.Empty:
      pass


def remove_socket_file(args: argparse.Namespace) -> Optional[int]:
  if os.path.exists(args.socket):
    try:
      os.remove(args.socket)
    except OSError as e:
      logging.error(e)
      return os.EX_UNAVAILABLE
  return None


def main(args: argparse.Namespace) -> int:
  """Main program takes args and returns status code.

  Args:
    args: Main program args.

  Returns:
    Status code.
  """
  if os.path.exists(args.socket):
    ret = remove_socket_file(args)
    if ret is not None:
      return ret
  else:
    dirname = os.path.dirname(args.socket)
    if not os.path.isdir(dirname):
      os.makedirs(dirname, 0o770)

  notifications = init_notifications(args)
  send_notification(notifications, 'Waiting', category='waiting', urgency=LOW)
  rename_tab('x')

  sel.register(sys.stdin, selectors.EVENT_READ, data=None)

  sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  sock.bind(args.socket)
  sock.listen(1)
  sock.setblocking(False)

  logging.info('Listening on %s', sock.getsockname())

  sel.register(sock, selectors.EVENT_READ, data=None)

  # This event is triggered when we shut everything down.
  shutdown = threading.Event()

  # This event is triggered when we want to kill the subprocess.
  interrupt = threading.Event()

  # This contains all of the scripts that need to be run.
  scripts: queue.Queue[str] = queue.Queue()

  proc = types.SimpleNamespace(interrupts=0)

  def reset_interrupts(retcode: Optional[int]):
    if retcode is not None:
      logging.debug('interrupts reset to 0')
      proc.interrupts = 0

  # This thread watches the queue and executes the scripts.
  t = threading.Thread(
      target=dequeue,
      args=(
          args,
          notifications,
          scripts,
          interrupt,
          shutdown,
          reset_interrupts,
      ),
  )
  t.start()

  try:
    while proc.interrupts < args.max_interrupts:
      interrupt.clear()
      try:
        events = sel.select()
        for key, mask in events:
          if key.fileobj == sys.stdin:
            sys.stdin.readline()
            if last_script:
              logging.info('Running last script again..')
              scripts.put(last_script)
          elif isinstance(key.fileobj, socket.socket):
            # If key.data is None, then you know it’s from the listening socket.
            if key.data is None:
              accept(key.fileobj)
            else:
              serve(key, mask, scripts)

      except KeyboardInterrupt:
        send_notification(
            notifications,
            'Keyboard Interrupt',
            category='interrupt',
            urgency=LOW,
        )
        if args.reset_and_clear:
          subprocess.call(['reset'])
          subprocess.call(['clear'])
        # print chr(27) + '[2J'
        proc.interrupts += 1
        logging.warning(
            f'Keyboard Interrupt ({proc.interrupts} of {args.max_interrupts})'
        )
        interrupt.set()

  except KeyboardInterrupt:
    print()
    logging.warning('KeyboardInterrupt')
    return os.EX_CANTCREAT

  finally:
    sel.close()
    shutdown.set()
    t.join()
    remove_socket_file(args)

  return os.EX_OK


if __name__ == '__main__':
  a = define_flags()
  logging.basicConfig(
      level=a.verbosity,
      datefmt='%Y/%m/%d %H:%M:%S',
      format='[%(asctime)s] %(levelname)s: %(message)s',
  )
  sys.exit(main(a))
