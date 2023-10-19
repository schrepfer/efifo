#!/usr/bin/env python3

"""Executes contents of FIFO file in a loop."""

import argparse
import datetime
import fcntl
import logging
import os
import stat
import subprocess
import sys
import time
import threading


def defineFlags():
  parser = argparse.ArgumentParser(description=__doc__)
  # See: http://docs.python.org/3/library/argparse.html
  parser.add_argument(
      '-v', '--verbosity',
      action='store',
      default=20,
      type=int,
      help='the logging verbosity',
      metavar='LEVEL')
  parser.add_argument(
      '-V', '--version',
      action='version',
      version='%(prog)s version 0.1')
  parser.add_argument(
      '--fifo',
      nargs=1,
      default=[os.getenv('EFIFO')],
      type=str,
      help='fifo file',
      metavar='FIFO')

  args = parser.parse_args()
  checkFlags(parser, args)
  return args

"""
import socket,os

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    os.remove("/tmp/socketname")
except OSError:
    pass
s.bind("/tmp/socketname")
s.listen(1)
conn, addr = s.accept()
while 1:
    data = conn.recv(1024)
    if not data: break
    conn.send(data)
conn.close()


# Echo client program
import socket

s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect("/tmp/socketname")
s.send(b'Hello, world')
data = s.recv(1024)
s.close()
print('Received ' + repr(data))
"""

"""
echo mytext | nc -U socket.sock
"""

def isFifoFile(path):
  if not os.path.exists(path):
    return False
  fs = os.stat(path)
  if not fs:
    return False
  return stat.S_ISFIFO(fs.st_mode)

def createFifoFile(fifo_path):
  if isFifoFile(fifo_path):
    return
  fifo_dir = os.path.dirname(fifo_path)
  if not os.path.isdir(fifo_dir):
    os.makedirs(fifo_dir, 0o770)
  if not os.path.exists(fifo_path):
    os.mkfifo(fifo_path, 0o700)

def checkFlags(parser, args):
  # See: http://docs.python.org/3/library/argparse.html#exiting-methods
  if not args.fifo or not args.fifo[0]:
    parser.error('--fifo required when $EFIFO unset')


CMD = os.path.splitext(os.path.basename(sys.argv[0]))[0]

NORMAL = 'normal'
LOW = 'low'
CRITICAL = 'critical'

def status(msg, *args, **kwargs):
  now = datetime.datetime.now()
  urgency = kwargs.get('urgency', NORMAL)
  category = kwargs.get('category', '')
  expire = kwargs.get('expire', '2000')
  if os.getenv('TMUX'):
    if urgency in {CRITICAL}:
      subprocess.call(['tmux', 'display-message', ' ' + msg % args])
  elif os.getenv('TERM').startswith('xterm'):
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


def shortStatus(msg, *args, **kwargs):
  if not os.getenv('TMUX'):
    return
  subprocess.call(['tmux', 'rename-window', '-t', os.getenv('TMUX_PANE'), msg % args])


IGNORED_COMMANDS = {'cd'}


def splitCommands(s):
  for cmds in (t.split(';') for t in s.splitlines()):
    for cmd in cmds:
      yield cmd


def firstCommand(s):
  for cmd in splitCommands(s):
    sp = cmd.split()
    if len(sp) == 0 or sp[0] in IGNORED_COMMANDS:
      continue
    return sp[0]
  return ''


def displayCommands(s):
  ret = []
  for cmd in splitCommands(s):
    sp = cmd.split()
    if len(sp) == 0 or sp[0] in IGNORED_COMMANDS:
      continue
    ret.append(cmd.strip())
  if not ret:
    return '<empty>'
  return '; '.join(ret)


def main(args):
  fifo = args.fifo[0]
  createFifoFile(fifo)
  lock_file = '%s.lock' % fifo
  locked = False
  try:
    logging.info('Acquiring lock file: %s', lock_file)
    fh = open(lock_file, 'w')
    fcntl.flock(fh, fcntl.LOCK_EX)
    logging.info('Lock acquire succeeded: %s', lock_file)

    locked = True
    interrupts = 0
    executions = 0

    status('Waiting', category='waiting', urgency=LOW)
    shortStatus('x')

    while interrupts < 3:
      try:
        while True:
          with open(fifo, 'r') as ffh:
            rr = ffh.read()
            executions += 1
            display = displayCommands(rr)
            status('Running: %s [%d]', display, executions, urgency=LOW)
            cmd = os.path.basename(firstCommand(rr))
            shortStatus('%s..' % cmd)
            start = time.time()
            p = subprocess.Popen(['bash', '-x'], stdin=subprocess.PIPE, text=True)

            def updateStatus():
              while p.poll() is None:
                status('Running: %s %ds [%d]',
                       display,
                       time.time() - start,
                       executions,
                       urgency=LOW)
                time.sleep(0.2)

            t = threading.Thread(target=updateStatus)
            t.start()

            p.communicate(input=rr)
            elapsed = time.time() - start
            t.join()

            if p.returncode == 0:
              shortStatus(cmd)
              status('DONE: %s [%d] %0.2fs',
                     display, p.returncode, elapsed,
                     category='done',
                     expire='15000',
                     urgency=NORMAL)
            else:
              shortStatus('%s!' % cmd)
              status('FAILED: %s [%d] %0.2fs',
                     display, p.returncode, elapsed,
                     category='failed',
                     urgency=CRITICAL,
                     expire='60000')
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


if __name__ == '__main__':
  args = defineFlags()
  logging.basicConfig(
      level=args.verbosity,
      datefmt='%Y/%m/%d %H:%M:%S',
      format='[%(asctime)s] %(levelname)s: %(message)s')
  sys.exit(main(args))
