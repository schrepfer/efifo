# efifo

Executes arbitrary scripts passed in via TCP socket.

NOTE: This was originally called efifo because it depended on fifo files to
handle this communication, but over the years this proved to be a bit
unreliable, and with the change to TCP sockets, it allowed me to implement a
process queue.
