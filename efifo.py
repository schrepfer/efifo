#!/usr/bin/env python3

"""Executes contents of FIFO file in a loop."""

from typing import Generator, Any, Optional, cast

import argparse
import datetime
import fcntl
import logging
import multiprocessing.dummy
import os
import queue
import select
import selectors
import socket
import stat
import subprocess
import sys
import threading
import time
import types


sel = selectors.DefaultSelector()


def define_flags() -> argparse.Namespace:
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v', '--verbosity',
      action='store',
      default=logging.INFO,
      type=int,
      help='the logging verbosity',
      metavar='LEVEL')
  parser.add_argument(
      '--max-interrupts',
      action='store',
      default=3,
      type=int,
      help='maximum number of interrupts before exiting',
      metavar='COUNT')
  parser.add_argument(
      '--polling-interval',
      action='store',
      default=0.1,
      type=float,
      help='number of seconds (float) between health checks',
      metavar='SECONDS',
  )
  parser.add_argument(
      '-V', '--version',
      action='version',
      version='%(prog)s version 0.1')
  parser.add_argument(
      '--fifo',
      nargs='?',
      default=os.getenv('EFIFO_FIFO', os.getenv('EFIFO')),
      type=str,
      help='fifo file',
      metavar='FIFO')
  parser.add_argument(
      '--socket',
      nargs='?',
      default=os.getenv('EFIFO_SOCKET'),
      type=str,
      help='socket file',
      metavar='SOCKET')

  args = parser.parse_args()
  check_flags(parser, args)
  return args


def check_flags(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not bool(args.fifo) ^ bool(args.socket):
    parser.error('--fifo or --socket required')


def is_fifo_file(path: str) -> bool:
  if not os.path.exists(path):
    return False
  fs = os.stat(path)
  if not fs:
    return False
  return stat.S_ISFIFO(fs.st_mode)


def create_fifo_file(fifo_path: str) -> None:
  if is_fifo_file(fifo_path):
    return
  fifo_dir = os.path.dirname(fifo_path)
  if not os.path.isdir(fifo_dir):
    os.makedirs(fifo_dir, 0o770)
  if not os.path.exists(fifo_path):
    os.mkfifo(fifo_path, 0o700)


CMD = os.path.splitext(os.path.basename(sys.argv[0]))[0]

NORMAL = 'normal'
LOW = 'low'
CRITICAL = 'critical'

def status(msg: str, *args: Any, **kwargs: Any) -> None:
  now = datetime.datetime.now()
  urgency = kwargs.get('urgency', NORMAL)
  category = kwargs.get('category', '')
  expire = kwargs.get('expire', '2000')
  if os.getenv('TMUX'):
    if urgency in {CRITICAL}:
      subprocess.call(['tmux', 'display-message', ' ' + msg % args])
  elif os.getenv('TERM', '').startswith('xterm'):
    sys.stdout.write('\x1B]0;[%s] %s: %s\x07\n' % (
        now.strftime('%H:%M:%S'),
        CMD,
        msg % args))
  if urgency in {NORMAL, CRITICAL}:
    subprocess.call(['notify-send',
                    "-u", urgency,
                    '-c', category,
                    '-t', expire,
                    'efifo: %s' % (msg % args)])


def short_status(msg: str, *args: Any, **kwargs: Any) -> None:
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
    if len(sp) == 0 or sp[0] in IGNORED_COMMANDS:
      continue
    return sp[0]
  return ''


def display_commands(s: str) -> str:
  ret = []
  for cmd in split_commands(s):
    sp = cmd.split()
    if len(sp) == 0 or sp[0] in IGNORED_COMMANDS:
      continue
    ret.append(cmd.strip())
  if not ret:
    return '<empty>'
  return '; '.join(ret)


executions = 0


def bash(args: argparse.Namespace,
         script: str,
         interrupt: Optional[threading.Event] = None) -> None:
  """Execute this bash script.

  Args:
    args: Flag arguments.
    script: Script to execute through `bash -x`.
    interrupt: When set, kill any running process.
  """
  global executions
  executions += 1
  display = display_commands(script)
  status('Running: %s [%d]', display, executions, urgency=LOW)
  cmd = os.path.basename(first_command(script))
  short_status('%s..' % cmd)
  start = time.time()
  p = subprocess.Popen(['bash', '-x'], stdin=subprocess.PIPE, text=True)

  def poll() -> None:
    while p.poll() is None:
      if interrupt and interrupt.is_set():
        logging.warning(f'Killing process {p}..')
        p.terminate()
        return
      status('Running: %s %ds [%d]',
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

  if p.returncode == 0:
    short_status(cmd)
    status('DONE: %s [%d] %0.2fs',
            display, p.returncode, elapsed,
            category='done',
            expire='15000',
            urgency=NORMAL)
  else:
    short_status('%s!' % cmd)
    status('FAILED: %s [%d] %0.2fs',
            display, p.returncode, elapsed,
            category='failed',
            urgency=CRITICAL,
            expire='60000')


def fifo_main(args: argparse.Namespace) -> int:
  create_fifo_file(args.fifo)
  lock_file = '%s.lock' % args.fifo
  locked = False
  try:
    logging.info('Acquiring lock file: %s', lock_file)
    fh = open(lock_file, 'w')
    fcntl.flock(fh, fcntl.LOCK_EX)
    logging.info('Lock acquire succeeded: %s', lock_file)

    locked = True
    interrupts = 0

    status('Waiting', category='waiting', urgency=LOW)
    short_status('x')

    while interrupts < 3:
      try:
        while True:
          with open(args.fifo, 'r') as ffh:
            bash(args, ffh.read())
          interrupts = 0

      except KeyboardInterrupt:
        status('Keyboard Interrupt', category='interrupt', urgency=LOW)
        subprocess.call(['reset'])
        subprocess.call(['clear'])
        # print chr(27) + '[2J'
        logging.warning('KeyboardInterrupt')
        interrupts += 1

  except IOError as e:
    logging.error(e)
    return os.EX_UNAVAILABLE

  except KeyboardInterrupt:
    print
    logging.warning('KeyboardInterrupt')
    return os.EX_CANTCREAT

  finally:
    if locked:
      fcntl.flock(fh, fcntl.LOCK_UN)
      logging.info('Lock released: %s', lock_file)

  return os.EX_OK


def accept(sock: socket.socket) -> None:
  conn, addr = sock.accept()
  if not addr:
    addr = conn.getsockname()
  logging.debug(f"Accepted connection on {addr}")
  conn.setblocking(False)
  data = types.SimpleNamespace(addr=addr, read=bytes(), write=bytes())
  sel.register(conn, selectors.EVENT_READ, data=data)


def serve(key: selectors.SelectorKey,
          mask: int,
          scripts: queue.Queue) -> None:
  """Serves the connection and adds to scripts Queue.

  Args:
    key:
    mask:
    scripts:

  Returns:
    None
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
      # Execute bash now..
      scripts.put(data.read.decode())
  if mask & selectors.EVENT_WRITE:
    raise NotImplementedError('EVENT_WRITE is not done')


def dequeue(args: argparse.Namespace,
            scripts: queue.Queue,
            interrupt: threading.Event,
            shutdown: threading.Event) -> None:
  """Dequeues events from the Queue and executes bash.

  Args:
    key:
    mask:
    scripts:

  Returns:
    None
  """
  while not shutdown.is_set():
    try:
      bash(args, scripts.get(timeout=args.polling_interval), interrupt=interrupt)
    except queue.Empty:
      pass


def socket_main(args: argparse.Namespace) -> int:
  """Socket main program takes args and returns status code.

  Args:
    args:

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

  status('Waiting', category='waiting', urgency=LOW)
  short_status('x')

  sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  sock.bind(args.socket)
  sock.listen(1)
  sock.setblocking(False)

  logging.info('Listening on %s', sock.getsockname())

  sel.register(sock, selectors.EVENT_READ, data=None)

  shutdown = threading.Event()
  interrupt = threading.Event()
  scripts: queue.Queue[str] = queue.Queue()
  t = threading.Thread(target=dequeue, args=(args, scripts, interrupt, shutdown))
  t.start()

  try:
    interrupts = 0
    while interrupts < args.max_interrupts:
      interrupt.clear()
      try:
        events = sel.select()
        for key, mask in events:
          # If key.data is None, then you know it’s from the listening socket.
          if key.data is None:
            #logging.info(f'accept(sock={key.fileobj})')
            accept(cast(socket.socket, key.fileobj))
          else:
            #logging.info(f'serve(key={key}, mask={mask})')
            serve(key, mask, scripts)
        # This should go in the Thread instead.
        interrupts = 0

      except KeyboardInterrupt:
        status('Keyboard Interrupt', category='interrupt', urgency=LOW)
        subprocess.call(['reset'])
        subprocess.call(['clear'])
        # print chr(27) + '[2J'
        interrupts += 1
        logging.warning(f'Keyboard Interrupt ({interrupts} of {args.max_interrupts})')
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


def main(args: argparse.Namespace) -> int:
  if args.fifo:
    return fifo_main(args)
  if args.socket:
    return socket_main(args)
  return os.EX_CANTCREAT


if __name__ == '__main__':
  args = define_flags()
  logging.basicConfig(
      level=args.verbosity,
      datefmt='%Y/%m/%d %H:%M:%S',
      format='[%(asctime)s] %(levelname)s: %(message)s')
  sys.exit(main(args))
