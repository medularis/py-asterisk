#!/usr/bin/env python
# vim: set expandtab shiftwidth=4:
"""
Python Interface for Asterisk Manager

This module provides a Python API for interfacing with the asterisk manager.

   import py_star.manager
   import sys

   def handle_shutdown(event, manager):
      print ("Received shutdown event")
      manager.close()
      # we could analyze the event and reconnect here

   def handle_event(event, manager):
      print ("Received event: %s" % event.name)

   manager = py_star.manager.Manager()
   try:
       # connect to the manager
       try:
          manager.connect('host') 
          manager.login('user', 'secret')

           # register some callbacks
           manager.register_event('Shutdown', handle_shutdown) # shutdown
           manager.register_event('*', handle_event)           # catch all

           # get a status report
           response = manager.status()

           manager.logoff()
       except py_star.manager.ManagerSocketException as err:
          errno, reason = err
          print ("Error connecting to the manager: %s" % reason)
          sys.exit(1)
       except py_star.manager.ManagerAuthException as reason:
          print ("Error logging in to the manager: %s" % reason)
          sys.exit(1)
       except py_star.manager.ManagerException as reason:
          print ("Error: %s" % reason)
          sys.exit(1)

   finally:
      # remember to clean up
      manager.close()

Remember all header, response, and event names are case sensitive.

Not all manager actions are implmented as of yet, feel free to add them
and submit patches.
"""
from __future__ import absolute_import, print_function, unicode_literals

import logging
import os
import socket
import sys
import threading

from . import compat_six as six
from six.moves import queue

logger = logging.getLogger(__name__)

EOL = '\r\n'


class _Message(object):

    def __init__(self):
        self.headers = {}

    def has_header(self, hname):
        """Check for a header"""
        return hname in self.headers

    def get_header(self, hname, defval=None):
        """Return the specified header"""
        return self.headers.get(hname, defval)

    def __getitem__(self, hname):
        """Return the specified header"""
        return self.headers[hname]

    def __repr__(self):
        return self.headers['Response']

# backwards compatibilty
_Msg = _Message


class ManagerMessage(_Message):

    """A manager interface message"""

    def __init__(self, response):
        super(ManagerMessage, self).__init__()

        # the raw response, straight from the horse's mouth:
        self.response = response
        self.data = ''
        self.multiheaders = {}

        # parse the response
        self.parse(response)

        # This is an unknown message, may happen if a command (notably
        # 'dialplan show something') contains a \n\r\n sequence in the
        # middle of output. We hope this happens only *once* during a
        # misbehaved command *and* the command ends with --END COMMAND--
        # in that case we return an Event.  Otherwise we asume it is
        # from a misbehaving command not returning a proper header (e.g.
        # IAXnetstats in Asterisk 1.4.X)
        # A better solution is probably to retain some knowledge of
        # commands sent and their expected return syntax. In that case
        # we could wait for --END COMMAND-- for 'command'.
        # B0rken in asterisk. This should be parseable without context.
        if 'Event' not in self.headers and 'Response' not in self.headers:
            # there are commands that return the ActionID but not
            # 'Response', e.g., IAXpeers in Asterisk 1.4.X
            if self.has_header('ActionID'):
                self.headers['Response'] = 'Generated Header'
                self.multiheaders['Response'] = ['Generated Header']
            elif '--END COMMAND--' in self.data:
                self.headers['Event'] = 'NoClue'
                self.multiheaders['Event'] = ['NoClue']
            else:
                self.headers['Response'] = 'Generated Header'
                self.multiheaders['Response'] = ['Generated Header']

    def parse(self, response):
        """Parse a manager message"""

        data = []
        for n, line in enumerate(response):
            # all valid header lines end in \r\n
            if not line.endswith('\r\n'):
                data.extend(response[n:])
                break
            try:
                k, v = (x.strip() for x in line.split(':', 1))
                if k not in self.multiheaders:
                    self.multiheaders[k] = []
                self.headers[k] = v
                self.multiheaders[k].append(v)
            except ValueError:
                # invalid header, start of multi-line data response
                data.extend(response[n:])
                break
        self.data = ''.join(data)

# backwards compatibilty
ManagerMsg = ManagerMessage


class Event(_Message):

    """Manager interface Events, __init__ expects and 'Event' message"""

    def __init__(self, message):
        super(Event, self).__init__()

        # store all of the event data
        self.message = message
        self.data = message.data
        self.headers = message.headers
        self.multiheaders = message.multiheaders

        # if this is not an event message we have a problem
        if not message.has_header('Event'):
            raise ManagerException('Trying to create event from non event message')

        # get the event name
        self.name = message.get_header('Event')

    def __repr__(self):
        return self.headers['Event']

    def get_action_id(self):
        return self.headers.get('ActionID', 0000)


class Manager(object):

    """Manager interface.

    Queue :attr:`errors_in_threads` stores messages about errors that
    happened in threads execution. Because there is no point in raising
    exceptions in threads, this is a way of letting the users of this
    class know that something bad has happened.

    .. warning::
       Errors happening in threads must be logged **and** a corresponding
       message added to :attr:`errors_in_threads`.

    """

    def __init__(self):
        self._sock = None     # our socket
        self.title = None     # set by received greeting
        self._connected = threading.Event()
        self._running = threading.Event()

        # our hostname
        self.hostname = socket.gethostname()
        # pid -- used for unique naming of ActionID
        self.pid = os.getpid()

        # our queues
        self._message_queue = queue.Queue()
        self._response_queue = queue.Queue()
        self._event_queue = queue.Queue()
        self.errors_in_threads = queue.Queue()

        # callbacks for events
        self._event_callbacks = {}

        self._response_waiters = []  # those who are waiting for a response

        # sequence stuff
        self._seqlock = threading.Lock()
        self._seq = 0

        # some threads
        self.message_thread = threading.Thread(target=self.message_loop)
        self.event_dispatch_thread = threading.Thread(target=self.event_dispatch)

        # TODO: this can be passed when threads are created
        self.message_thread.setDaemon(True)
        self.event_dispatch_thread.setDaemon(True)

        # special sentinel value: when placed in a queue, its consumers
        # know they have to terminate
        self._sentinel = object()

    def __del__(self):
        self.close()

    def is_connected(self):
        """
        Check if we are connected or not.
        """
        return self._connected.isSet()

    # backwards compatibilty
    connected = is_connected

    def is_running(self):
        """Return whether we are running or not."""
        return self._running.isSet()

    def next_seq(self):
        """Return the next number in the sequence, this is used for ActionID"""
        self._seqlock.acquire()
        try:
            return self._seq
        finally:
            self._seq += 1
            self._seqlock.release()

    def send_action(self, cdict=None, **kwargs):
        """
        Send a command to the manager

        If a list is passed to the cdict argument, each item in the list will
        be sent to asterisk under the same header in the following manner:

        cdict = {"Action": "Originate",
                 "Variable": ["var1=value", "var2=value"]}
        send_action(cdict)

        ...

        Action: Originate
        Variable: var1=value
        Variable: var2=value
        """
        cdict = cdict or {}

        if not self.is_connected():
            raise ManagerException("Not connected")

        # fill in our args
        cdict.update(kwargs)

        # set the action id
        if 'ActionID' not in cdict:
            cdict['ActionID'] = '%s-%04s-%08x' % (
                self.hostname, self.pid, self.next_seq())
        clist = []

        # generate the command
        for key, value in cdict.items():
            if isinstance(value, list):
                for item in value:
                    item = tuple([key, item])
                    clist.append('%s: %s' % item)
            else:
                item = tuple([key, value])
                clist.append('%s: %s' % item)
        clist.append(EOL)
        command = EOL.join(clist)

        # lock the socket and send our command
        try:
            self._sock.write(command.encode('utf-8'))
            self._sock.flush()
            logger.debug("Wrote to socket file this command:\n%s" % command)
        except socket.error as err:
            errno, reason = err
            raise ManagerSocketException(errno, reason)

        self._response_waiters.insert(0, 1)
        response = self._response_queue.get()
        self._response_waiters.pop(0)

        # if we got the sentinel value as a response we are done
        if response is self._sentinel:
            raise ManagerSocketException(0, 'Connection Terminated')

        return response

    def _receive_data(self):
        """
        Read the response from a command.
        """

        multiline = False
        wait_for_marker = False
        eolcount = 0
        # loop while we are sill running and connected
        while self.is_running() and self.is_connected():
            try:
                lines = []
                for line in self._sock:
                    line = line.decode('utf-8')
                    # check to see if this is the greeting line
                    if not self.title and '/' in line and ':' not in line:
                        # store the title of the manager we are connecting to:
                        self.title = line.split('/')[0].strip()
                        # store the version of the manager we are connecting to:
                        self.version = line.split('/')[1].strip()
                        # fake message header
                        lines.append('Response: Generated Header\r\n')
                        lines.append(line)
                        logger.debug("Fake message header. Will exit the "
                                     "socket file iteration loop")
                        break
                    # If the line is EOL marker we have a complete message.
                    # Some commands are broken and contain a \n\r\n
                    # sequence, in the case wait_for_marker is set, we
                    # have such a command where the data ends with the
                    # marker --END COMMAND--, so we ignore embedded
                    # newlines until we see that marker
                    if line == EOL and not wait_for_marker:
                        multiline = False

                        # we split the break conditions because they are of very
                        # different nature and we'd like more fine-grained logs
                        if lines:
                            logger.debug("Have %s lines. Will exit the socket "
                                         "file iteration loop" % len(lines))
                            break
                        if not self.is_connected():
                            logger.warning("Not connected. Will exit the "
                                           "socket file iteration loop")
                            break

                        # ignore empty lines at start
                        continue
                    lines.append(line)
                    # line not ending in \r\n or without ':' isn't a
                    # valid header and starts multiline response
                    if not line.endswith('\r\n') or ':' not in line:
                        multiline = True
                    # Response: Follows indicates we should wait for end
                    # marker --END COMMAND--
                    if (not multiline and line.startswith('Response') and
                            line.split(':', 1)[1].strip() == 'Follows'):
                        wait_for_marker = True
                    # same when seeing end of multiline response
                    if multiline and line.startswith('--END COMMAND--'):
                        wait_for_marker = False
                        multiline = False
                    if not self.is_connected():
                        logger.info("Not connected. Will exit the "
                                    "socket file iteration loop")
                        break
                else:
                    # EOF during reading
                    logger.error("Problem reading socket file")
                    self._sock.close()
                    logger.info("Closed socket file")
                    self._connected.clear()
                # if we have a message append it to our queue
                # else notify `message_loop` that it has to finish
                if lines:
                    if self.is_connected():
                        self._message_queue.put(lines)
                    else:
                        msg = "Received lines but are not connected"
                        logger.warning(msg)
                        self._message_queue.put(self._sentinel)
                        self.errors_in_threads.put(msg)
                else:
                    msg = "No lines received"
                    logger.warning(msg)
                    self._message_queue.put(self._sentinel)
                    self.errors_in_threads.put(msg)
            except socket.error:
                msg = "Socket error"
                logger.exception(msg)
                self._sock.close()
                logger.info("Closed socket file")
                self._connected.clear()
                # notify `message_loop` that it has to finish
                self._message_queue.put(self._sentinel)
                self.errors_in_threads.put(msg)

    def register_event(self, event, function):
        """
        Register a callback for the specfied event.
        If a callback function returns True, no more callbacks for that
        event will be executed.
        """

        # get the current value, or an empty list
        # then add our new callback
        current_callbacks = self._event_callbacks.get(event, [])
        current_callbacks.append(function)
        self._event_callbacks[event] = current_callbacks

    def unregister_event(self, event, function):
        """
        Unregister a callback for the specified event.
        """
        current_callbacks = self._event_callbacks.get(event, [])
        current_callbacks.remove(function)
        self._event_callbacks[event] = current_callbacks

    def message_loop(self):
        """
        The method for the event thread.
        This actually recieves all types of messages and places them
        in the proper queues.
        """

        # start a thread to receive data
        t = threading.Thread(target=self._receive_data)
        t.setDaemon(True)
        t.start()

        try:
            # loop getting messages from the queue
            while self.is_running():
                # get/wait for messages
                data = self._message_queue.get()

                # if we got the sentinel value as our message we are done
                # (have to notify `_event_queue` once, and `_response_queue`
                #  as many times as the length of `_response_waiters`)
                if data is self._sentinel:
                    logger.info("Got sentinel object. Will notify the other "
                                "queues and then break this loop")
                    # notify `event_dispatch` that it has to finish
                    self._event_queue.put(self._sentinel)
                    for waiter in self._response_waiters:
                        self._response_queue.put(self._sentinel)
                    break

                # parse the data
                message = ManagerMessage(data)

                # check if this is an event message
                if message.has_header('Event'):
                    self._event_queue.put(Event(message))
                # check if this is a response
                elif message.has_header('Response'):
                    self._response_queue.put(message)
                else:
                    # notify `_response_queue`'s consumer (`send_action`)
                    # that it has to finish
                    msg = "No clue what we got\n%s" % message.data
                    logger.error(msg)
                    self._response_queue.put(self._sentinel)
                    self.errors_in_threads.put(msg)
        except Exception:
            logger.exception("Exception in the message loop")
            six.reraise(*sys.exc_info())
        finally:
            # wait for our data receiving thread to exit
            logger.debug("Waiting for our data-receiving thread to exit")
            t.join()

    def event_dispatch(self):
        """This thread is responsible for dispatching events"""

        # loop dispatching events
        while self.is_running():
            # get/wait for an event
            ev = self._event_queue.get()

            # if we got the sentinel value as an event we are done
            if ev is self._sentinel:
                logger.info("Got sentinel object. Will break dispatch loop")
                break

            # dispatch our events

            # first build a list of the functions to execute
            callbacks = (self._event_callbacks.get(ev.name, []) +
                         self._event_callbacks.get('*', []))

            # now execute the functions  
            for callback in callbacks:
                if callback(ev, self):
                    break

    def connect(self, host, port=5038):
        """Connect to the manager interface"""

        if self.is_connected():
            raise ManagerException('Already connected to manager')

        # make sure host is a string
        assert isinstance(host, six.string_types)

        port = int(port)  # make sure port is an int

        # create our socket and connect
        try:
            _sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _sock.connect((host, port))
            self._sock = _sock.makefile(mode='rwb')
            _sock.close()
        except socket.error as err:
            errno, reason = err
            raise ManagerSocketException(errno, reason)

        # we are connected and running
        self._connected.set()
        self._running.set()

        # start the event thread
        self.message_thread.start()

        # start the event dispatching thread
        self.event_dispatch_thread.start()

        # get our initial connection response
        response = self._response_queue.get()

        # if we got the sentinel value as a response then something went awry
        if response is self._sentinel:
            raise ManagerSocketException(0, "Connection Terminated")

        return response

    def close(self):
        """Shutdown the connection to the manager"""

        # if we are still running, logout
        if self.is_running() and self.is_connected():
            logger.debug("Logoff before closing (we are running and connected)")
            self.logoff()

        if self.is_running():
            # notify `message_loop` that it has to finish
            logger.debug("Notify message loop that it has to finish")
            self._message_queue.put(self._sentinel)

            # wait for the event thread to exit
            logger.debug("Waiting for `message_thread` to exit")
            self.message_thread.join()

            # make sure we do not join our self (when close is called from event handlers)
            if threading.currentThread() != self.event_dispatch_thread:
                # wait for the dispatch thread to exit
                logger.debug("Waiting for `event_dispatch_thread` to exit")
                self.event_dispatch_thread.join()

        self._running.clear()

# Manager actions

    def login(self, username, secret):
        """Login to the manager, throws ManagerAuthException when login falis.

        :return: action response

        """
        cdict = {
            'Action': 'Login',
            'Username': username,
            'Secret': secret,
        }
        response = self.send_action(cdict)

        if response.get_header('Response') == 'Error':
            raise ManagerAuthException(response.get_header('Message'))

        return response

    def ping(self):
        """Send a ping action to the manager.

        :return: action response

        """
        cdict = {'Action': 'Ping'}
        return self.send_action(cdict)

    def logoff(self):
        """Logoff from the manager.

        :return: action response

        """
        cdict = {'Action': 'Logoff'}
        return self.send_action(cdict)

    def hangup(self, channel):
        """Hangup the specified channel.

        :return: action response

        """
        cdict = {
            'Action': 'Hangup',
            'Channel': channel,
        }
        return self.send_action(cdict)

    def status(self, channel=''):
        """Get a status message from asterisk.

        :return: action response

        """
        cdict = {
            'Action': 'Status',
            'Channel': channel,
        }
        return self.send_action(cdict)

    def redirect(self, channel, exten, priority='1', extra_channel='', context=''):
        """Redirect a channel.

        :return: action response

        """
        cdict = {
            'Action': 'Redirect',
            'Channel': channel,
            'Exten': exten,
            'Priority': priority,
        }
        if context:
            cdict['Context'] = context
        if extra_channel:
            cdict['ExtraChannel'] = extra_channel

        return self.send_action(cdict)

    def originate(self, channel, exten, context='', priority='', timeout='',
                  caller_id='', async=False, account='', variables=None):
        """Originate a call.

        :return: action response

        """
        variables = variables or {}

        cdict = {
            'Action': 'Originate',
            'Channel': channel,
            'Exten': exten,
        }

        if context:
            cdict['Context'] = context
        if priority:
            cdict['Priority'] = priority
        if timeout:
            cdict['Timeout'] = timeout
        if caller_id:
            cdict['CallerID'] = caller_id
        if async:
            cdict['Async'] = 'yes'
        if account:
            cdict['Account'] = account
        if variables:
            cdict['Variable'] = ['='.join((str(key), str(value)))
                                 for key, value in variables.items()]

        return self.send_action(cdict)

    def mailbox_status(self, mailbox):
        """Get the status of the specfied mailbox.

        :return: action response

        """
        cdict = {
            'Action': 'MailboxStatus',
            'Mailbox': mailbox,
        }
        return self.send_action(cdict)

    def command(self, command):
        """Execute a command.

        :return: action response

        """
        cdict = {
            'Action': 'Command',
            'Command': command,
        }
        return self.send_action(cdict)

    def extension_state(self, exten, context):
        """Get the state of an extension.

        :return: action response

        """
        cdict = {
            'Action': 'ExtensionState',
            'Exten': exten,
            'Context': context,
        }
        return self.send_action(cdict)

    def playdtmf(self, channel, digit):
        """Plays a dtmf digit on the specified channel.

        :return: action response

        """
        cdict = {
            'Action': 'PlayDTMF',
            'Channel': channel,
            'Digit': digit,
        }
        return self.send_action(cdict)

    def absolute_timeout(self, channel, timeout):
        """Set an absolute timeout on a channel.

        :return: action response

        """
        cdict = {
            'Action': 'AbsoluteTimeout',
            'Channel': channel,
            'Timeout': timeout,
        }
        return self.send_action(cdict)

    def mailbox_count(self, mailbox):
        cdict = {
            'Action': 'MailboxCount',
            'Mailbox': mailbox,
        }
        return self.send_action(cdict)

    def sippeers(self):
        cdict = {'Action': 'Sippeers'}
        return self.send_action(cdict)

    def sipshowpeer(self, peer):
        cdict = {
            'Action': 'SIPshowpeer',
            'Peer': peer,
        }
        return self.send_action(cdict)


class ManagerException(Exception):
    pass


class ManagerSocketException(ManagerException):
    pass


class ManagerAuthException(ManagerException):
    pass
