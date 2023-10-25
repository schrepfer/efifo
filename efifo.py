#!/usr/bin/env python3

"""Executes arbitrary scripts passed in via TCP socket."""

import argparse
import datetime
import logging
import os
import queue
import selectors
import socket
import subprocess
import sys
import threading
import time
import types

from typing import Any, Callable, Generator, Optional, cast


sel = selectors.DefaultSelector()


def define_flags() -> argparse.Namespace:
  """Defines the flags for this program."""
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v', '--verbosity',
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
      '-V', '--version',
      action='version',
      version='%(prog)s version 0.1')
  parser.add_argument(
      '--socket',
      nargs='?',
      default=os.getenv('EFIFO_SOCKET', os.getenv('EFIFO')),
      type=str,
      help='socket file',
      metavar='SOCKET')

  args = parser.parse_args()
  check_flags(parser, args)
  return args


def check_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not args.socket:
    parser.error('--socket file required')


CMD = os.path.splitext(os.path.basename(sys.argv[0]))[0]

NORMAL = 'normal'
LOW = 'low'
CRITICAL = 'critical'


def send_notification(
    msg: str,
    *args: Any,
    urgency: str = NORMAL,
    category: str = '',
    expire: int = 2000) -> None:
  """Send notifications in the most visible way.

  Args:
    msg: The message to show.
    *args: Extra string formatting files for msg.
    urgency: One of NORMAL, LOW or CRITICAL.
    category: A category for this notification.
    expire: Number of seconds to expire.
  """
  now = datetime.datetime.now()
  if os.getenv('TMUX'):
    if urgency in {CRITICAL}:
      subprocess.call(['tmux', 'display-message', ' ' + msg % args])
  elif os.getenv('TERM', '').startswith('xterm'):
    sys.stdout.write('\x1B]0;[{time}] {cmd}: {message}\x07\n'.format(
        time=now.strftime('%H:%M:%S'),
        cmd=CMD,
        message=msg % args,
    ))
  if urgency in {NORMAL, CRITICAL}:
    subprocess.call(['notify-send',
                     '-u', urgency,
                     '-c', category,
                     '-t', str(expire),
                     'efifo: %s' % (msg % args)])


def short_status(msg: str, *args: Any) -> None:
  """Display a short status message (to TMUX).

  Args:
    msg: The message to show.
    *args: Extra string formatting files for msg.
  """
  if not os.getenv('TMUX'):
    return
  subprocess.call(['tmux', 'rename-window', '-t', os.getenv('TMUX_PANE', ''), msg % args])


IGNORED_COMMANDS = {'cd'}


def split_commands(s: str) -> Generator[str, None, None]:
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


def execute_bash(args: argparse.Namespace,
                 script: str,
                 interrupt: Optional[threading.Event] = None) -> Optional[int]:
  """Execute this bash script.

  Args:
    args: Main program args.
    script: Script to execute through `bash -x`.
    interrupt: When set, kill any running process.

  Returns:
    Status code.
  """
  global executions
  executions += 1
  display = display_commands(script)
  send_notification('Running: %s [%d]', display, executions, urgency=LOW)
  cmd = os.path.basename(first_command(script))
  short_status('%s..' % cmd)
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
      send_notification('Running: %s %ds [%d]',
                        display,
                        time.time() - start,
                        executions,
                        urgency=LOW)
      time.sleep(args.polling_interval)

  t = threading.Thread(target=poll)
  t.start()

  p.communicate(input=script)
  elapsed = time.time() - start
  t.join()

  if proc.killed:
    short_status(cmd)
    send_notification('KILLED: %s %0.2fs',
                      display,
                      elapsed,
                      category='done',
                      expire=15000,
                      urgency=NORMAL)
    return None

  if p.returncode == 0:
    short_status(cmd)
    send_notification('DONE: %s [%d] %0.2fs',
                      display,
                      p.returncode,
                      elapsed,
                      category='done',
                      expire=15000,
                      urgency=NORMAL)
  else:
    short_status('%s!' % cmd)
    send_notification('FAILED: %s [%d] %0.2fs',
                      display,
                      p.returncode,
                      elapsed,
                      category='failed',
                      urgency=CRITICAL,
                      expire=60000)

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


def serve(key: selectors.SelectorKey,
          mask: int,
          scripts: queue.Queue[str]) -> None:
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


def dequeue(args: argparse.Namespace,
            scripts: queue.Queue[str],
            interrupt: threading.Event,
            shutdown: threading.Event,
            callback: Callable[[Optional[int]], None]) -> None:
  """Dequeues events from the Queue and executes bash.

  Args:
    args: Main program args.
    scripts: The scripts queue.
    interrupt: When set, kill any running process.
    shutdown: When set, shutdown this thread.
    callback: Callback function that takes the command status code as input.
  """
  while not shutdown.is_set():
    try:
      retcode = execute_bash(args, scripts.get(timeout=args.polling_interval), interrupt=interrupt)
      callback(retcode)
    except queue.Empty:
      pass


def main(args: argparse.Namespace) -> int:
  """Main program takes args and returns status code.

  Args:
    args: Main program args.

  Returns:
    Status code.
  """
  if os.path.exists(args.socket):
    try:
      os.remove(args.socket)
    except OSError as e:
      logging.error(e)
      return os.EX_UNAVAILABLE
  else:
    dirname = os.path.dirname(args.socket)
    if not os.path.isdir(dirname):
      os.makedirs(dirname, 0o770)

  send_notification('Waiting', category='waiting', urgency=LOW)
  short_status('x')

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
  t = threading.Thread(target=dequeue, args=(
      args,
      scripts,
      interrupt,
      shutdown,
      reset_interrupts,
  ))
  t.start()

  try:
    while proc.interrupts < args.max_interrupts:
      interrupt.clear()
      try:
        events = sel.select()
        for key, mask in events:
          # If key.data is None, then you know it’s from the listening socket.
          if key.data is None:
            accept(cast(socket.socket, key.fileobj))
          else:
            serve(key, mask, scripts)

      except KeyboardInterrupt:
        send_notification('Keyboard Interrupt', category='interrupt', urgency=LOW)
        if args.reset_and_clear:
          subprocess.call(['reset'])
          subprocess.call(['clear'])
        # print chr(27) + '[2J'
        proc.interrupts += 1
        logging.warning(f'Keyboard Interrupt ({proc.interrupts} of {args.max_interrupts})')
        interrupt.set()

  except KeyboardInterrupt:
    print()
    logging.warning('KeyboardInterrupt')
    return os.EX_CANTCREAT

  finally:
    sel.close()
    shutdown.set()
    t.join()

  return os.EX_OK


if __name__ == '__main__':
  a = define_flags()
  logging.basicConfig(
      level=a.verbosity,
      datefmt='%Y/%m/%d %H:%M:%S',
      format='[%(asctime)s] %(levelname)s: %(message)s')
  sys.exit(main(a))
