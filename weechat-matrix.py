# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import json
import socket
import ssl
import time
import datetime
import re

# pylint: disable=redefined-builtin
from builtins import bytes

from collections import deque, Mapping, Iterable, namedtuple
from operator import itemgetter
from enum import Enum, unique
from functools import wraps

# pylint: disable=unused-import
from typing import (List, Set, Dict, Tuple, Text, Optional, AnyStr, Deque, Any)

from http_parser.pyparser import HttpParser

# pylint: disable=import-error
import weechat

WEECHAT_SCRIPT_NAME        = "matrix"                               # type: unicode
WEECHAT_SCRIPT_DESCRIPTION = "matrix chat plugin"                   # type: unicode
WEECHAT_SCRIPT_AUTHOR      = "Damir Jelić <poljar@termina.org.uk>"  # type: unicode
WEECHAT_SCRIPT_VERSION     = "0.1"                                  # type: unicode
WEECHAT_SCRIPT_LICENSE     = "MIT"                                  # type: unicode

MATRIX_API_PATH = "/_matrix/client/r0"  # type: unicode

SERVERS        = dict()  # type: Dict[unicode, MatrixServer]
CONFIG         = None    # type: weechat.config
GLOBAL_OPTIONS = None    # type: PluginOptions

NICK_GROUP_HERE = "0|Here"


# Unicode handling
def encode_to_utf8(data):
    if isinstance(data, unicode):
        return data.encode('utf-8')
    if isinstance(data, bytes):
        return data
    elif isinstance(data, Mapping):
        return type(data)(map(encode_to_utf8, data.iteritems()))
    elif isinstance(data, Iterable):
        return type(data)(map(encode_to_utf8, data))
    return data


def decode_from_utf8(data):
    if isinstance(data, bytes):
        return data.decode('utf-8')
    if isinstance(data, unicode):
        return data
    elif isinstance(data, Mapping):
        return type(data)(map(decode_from_utf8, data.iteritems()))
    elif isinstance(data, Iterable):
        return type(data)(map(decode_from_utf8, data))
    return data


def utf8_decode(function):
    """
    Decode all arguments from byte strings to unicode strings. Use this for
    functions called from outside of this script, e.g. callbacks from weechat.
    """
    @wraps(function)
    def wrapper(*args, **kwargs):
        return function(*decode_from_utf8(args), **decode_from_utf8(kwargs))
    return wrapper


class WeechatWrapper(object):
    def __init__(self, wrapped_class):
        self.wrapped_class = wrapped_class

    # Helper method used to encode/decode method calls.
    def wrap_for_utf8(self, method):
        def hooked(*args, **kwargs):
            result = method(*encode_to_utf8(args), **encode_to_utf8(kwargs))
            # Prevent wrapped_class from becoming unwrapped
            if result == self.wrapped_class:
                return self
            return decode_from_utf8(result)
        return hooked

    # Encode and decode everything sent to/received from weechat. We use the
    # unicode type internally in wee-slack, but has to send utf8 to weechat.
    def __getattr__(self, attr):
        orig_attr = self.wrapped_class.__getattribute__(attr)
        if callable(orig_attr):
            return self.wrap_for_utf8(orig_attr)
        return decode_from_utf8(orig_attr)

    # Ensure all lines sent to weechat specify a prefix. For lines after the
    # first, we want to disable the prefix, which is done by specifying a
    # space.
    def prnt_date_tags(self, buffer, date, tags, message):
        message = message.replace("\n", "\n \t")
        return self.wrap_for_utf8(self.wrapped_class.prnt_date_tags)(
            buffer,
            date,
            tags,
            message
        )


@unique
class MessageType(Enum):
    LOGIN    = 0
    SYNC     = 1
    SEND     = 2
    STATE    = 3
    REDACT   = 4
    ROOM_MSG = 5


@unique
class RequestType(Enum):
    GET    = 0
    POST   = 1
    PUT    = 2


@unique
class RedactType(Enum):
    STRIKETHROUGH = 0
    NOTICE        = 1
    DELETE        = 2


@unique
class ServerBufferType(Enum):
    MERGE_CORE  = 0
    MERGE       = 1
    INDEPENDENT = 2


Option = namedtuple(
    'Option', [
        'name',
        'type',
        'string_values',
        'min',
        'max',
        'value',
        'description'
    ])


class PluginOptions:
    def __init__(self):
        self.redaction_type  = RedactType.STRIKETHROUGH     # type: RedactType
        self.look_server_buf = ServerBufferType.MERGE_CORE  # type: ServerBufferType

        self.redaction_comp_len = 50  # type: int
        self.msg_redact = ""

        self.options = dict()  # type: Dict[unicode, weechat.config_option]


class HttpResponse:
    def __init__(self, status, headers, body):
        self.status  = status   # type: int
        self.headers = headers  # type: Dict[unicode, unicode]
        self.body    = body     # type: bytes


class HttpRequest:
    def __init__(
            self,
            request_type,                        # type: RequestType
            host,                                # type: unicode
            port,                                # type: int
            location,                            # type: unicode
            data=None,                           # type: Dict[unicode, Any]
            user_agent='weechat-matrix/{version}'.format(
                version=WEECHAT_SCRIPT_VERSION)  # type: unicode
    ):
        # type: (...) -> None
        host_string   = ':'.join([host, str(port)])

        user_agent    = 'User-Agent: {agent}'.format(agent=user_agent)
        host_header   = 'Host: {host}'.format(host=host_string)
        request_list  = []             # type: List[unicode]
        accept_header = 'Accept: */*'  # type: unicode
        end_separator = '\r\n'         # type: unicode
        payload       = None           # type: unicode

        if request_type == RequestType.GET:
            get = 'GET {location} HTTP/1.1'.format(location=location)
            request_list  = [get, host_header,
                             user_agent, accept_header, end_separator]

        elif (request_type == RequestType.POST or
              request_type == RequestType.PUT):

            json_data     = json.dumps(data, separators=(',', ':'))

            if request_type == RequestType.POST:
                method = "POST"
            else:
                method = "PUT"

            request_line = '{method} {location} HTTP/1.1'.format(
                method=method,
                location=location
            )

            type_header   = 'Content-Type: application/x-www-form-urlencoded'
            length_header = 'Content-Length: {length}'.format(
                length=len(json_data)
            )

            request_list  = [request_line, host_header,
                             user_agent, accept_header,
                             length_header, type_header, end_separator]
            payload       = json_data

        request = '\r\n'.join(request_list)

        self.request = request
        self.payload = payload


def get_transaction_id(server):
    # type: (MatrixServer) -> int
    transaction_id = server.transaction_id
    server.transaction_id += 1
    return transaction_id


class MatrixMessage:
    def __init__(
            self,
            server,           # type: MatrixServer
            message_type,     # type: MessageType
            room_id=None,     # type: unicode
            extra_id=None,  # type: unicode
            data=None,        # type: Dict[unicode, Any]
            extra_data=None   # type: Dict[unicode, Any]
    ):
        # type: (...) -> None
        self.type       = message_type  # MessageType
        self.request    = None          # HttpRequest
        self.response   = None          # HttpResponse
        self.extra_data = extra_data    # Dict[unicode, Any]

        if message_type == MessageType.LOGIN:
            path = ("{api}/login").format(api=MATRIX_API_PATH)
            self.request = HttpRequest(
                RequestType.POST,
                server.address,
                server.port,
                path,
                data
            )

        elif message_type == MessageType.SYNC:
            # TODO the limit should be configurable matrix.network.sync_limit
            sync_filter = {
                "room": {
                    "timeline": {"limit": 10}
                }
            }

            path = ("{api}/sync?access_token={access_token}&"
                    "filter={sync_filter}").format(
                        api=MATRIX_API_PATH,
                        access_token=server.access_token,
                        sync_filter=json.dumps(sync_filter,
                                               separators=(',', ':')))

            if server.next_batch:
                path = path + '&since={next_batch}'.format(
                    next_batch=server.next_batch)

            self.request = HttpRequest(
                RequestType.GET,
                server.address,
                server.port,
                path
            )

        elif message_type == MessageType.SEND:
            path = ("{api}/rooms/{room}/send/m.room.message?"
                    "access_token={access_token}").format(
                        api=MATRIX_API_PATH,
                        room=room_id,
                        access_token=server.access_token)

            self.request = HttpRequest(
                RequestType.POST,
                server.address,
                server.port,
                path,
                data
            )

        elif message_type == MessageType.STATE:
            path = ("{api}/rooms/{room}/state/{event_type}?"
                    "access_token={access_token}").format(
                        api=MATRIX_API_PATH,
                        room=room_id,
                        event_type=extra_id,
                        access_token=server.access_token)

            self.request = HttpRequest(
                RequestType.PUT,
                server.address,
                server.port,
                path,
                data
            )

        elif message_type == MessageType.REDACT:
            path = ("{api}/rooms/{room}/redact/{event_id}/{tx_id}?"
                    "access_token={access_token}").format(
                        api=MATRIX_API_PATH,
                        room=room_id,
                        event_id=extra_id,
                        tx_id=get_transaction_id(server),
                        access_token=server.access_token)

            self.request = HttpRequest(
                RequestType.PUT,
                server.address,
                server.port,
                path,
                data
            )

        elif message_type == MessageType.ROOM_MSG:
            path = ("{api}/rooms/{room}/messages?from={prev_batch}&"
                    "dir=b&limit={message_limit}&"
                    "access_token={access_token}").format(
                        api=MATRIX_API_PATH,
                        room=room_id,
                        prev_batch=extra_id,
                        message_limit=10,
                        access_token=server.access_token)
            self.request = HttpRequest(
                RequestType.GET,
                server.address,
                server.port,
                path,
            )


class MatrixRoom:
    def __init__(self, room_id):
        # type: (unicode) -> None
        self.room_id      = room_id    # type: unicode
        self.alias        = room_id    # type: unicode
        self.topic        = ""         # type: unicode
        self.topic_author = ""         # type: unicode
        self.topic_date   = None       # type: datetime.datetime
        self.prev_batch   = ""         # type: unicode


def key_from_value(dictionary, value):
    # type: (Dict[unicode, Any], Any) -> unicode
    return list(dictionary.keys())[list(dictionary.values()).index(value)]


@utf8_decode
def server_config_change_cb(server_name, option):
    # type: (unicode, weechat.config_option) -> int
    server = SERVERS[server_name]
    option_name = None

    # The function config_option_get_string() is used to get differing
    # properties from a config option, sadly it's only available in the plugin
    # API of weechat.
    option_name = key_from_value(server.options, option)

    if option_name == "address":
        value = W.config_string(option)
        server.address = value
    elif option_name == "autoconnect":
        value = W.config_boolean(option)
        server.autoconnect = value
    elif option_name == "port":
        value = W.config_integer(option)
        server.port = value
    elif option_name == "username":
        value = W.config_string(option)
        server.user = value
    elif option_name == "password":
        value = W.config_string(option)
        server.password = value
    else:
        pass

    return 1


class MatrixServer:
    # pylint: disable=too-many-instance-attributes
    def __init__(self, name, config_file):
        # type: (unicode, weechat.config) -> None
        self.name            = name     # type: unicode
        self.address         = ""       # type: unicode
        self.port            = 8448     # type: int
        self.options         = dict()   # type: Dict[unicode, weechat.config]

        self.user            = ""       # type: unicode
        self.password        = ""       # type: unicode

        self.rooms           = dict()   # type: Dict[unicode, MatrixRoom]
        self.buffers         = dict()   # type: Dict[unicode, weechat.buffer]
        self.server_buffer   = None     # type: weechat.buffer
        self.fd_hook         = None     # type: weechat.hook
        self.timer_hook      = None     # type: weechat.hook
        self.numeric_address = ""       # type: unicode

        self.autoconnect     = False                         # type: bool
        self.connected       = False                         # type: bool
        self.connecting      = False                         # type: bool
        self.reconnect_count = 0                             # type: int
        self.socket          = None                          # type: ssl.SSLSocket
        self.ssl_context     = ssl.create_default_context()  # type: ssl.SSLContext

        self.access_token    = None                          # type: unicode
        self.next_batch      = None                          # type: unicode
        self.transaction_id  = 0                             # type: int

        self.http_parser = HttpParser()                  # type: HttpParser
        self.http_buffer = []                            # type: List[bytes]

        # Queue of messages we need to send off.
        self.send_queue    = deque()  # type: Deque[MatrixMessage]
        # Queue of messages we send off and are waiting a response for
        self.receive_queue = deque()  # type: Deque[MatrixMessage]
        self.message_queue = deque()  # type: Deque[MatrixMessage]
        self.ignore_event_list = []   # type: List[unicode]

        self._create_options(config_file)

        # FIXME Don't set insecure
        self._set_insecure()

    # TODO remove this
    def _set_insecure(self):
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

    def _create_options(self, config_file):
        options = [
            Option(
                'autoconnect', 'boolean', '', 0, 0, 'off',
                (
                    "automatically connect to the matrix server when weechat "
                    "is starting"
                )
            ),
            Option(
                'address', 'string', '', 0, 0, '',
                "Hostname or IP address for the server"
            ),
            Option(
                'port', 'integer', '', 0, 65535, '8448',
                "Port for the server"
            ),
            Option(
                'username', 'string', '', 0, 0, '',
                "Username to use on server"
            ),
            Option(
                'password', 'string', '', 0, 0, '',
                "Password for server"
            ),
        ]

        section = W.config_search_section(config_file, 'server')

        for option in options:
            option_name = "{server}.{option}".format(
                server=self.name, option=option.name)

            self.options[option.name] = W.config_new_option(
                config_file, section, option_name,
                option.type, option.description, option.string_values,
                option.min, option.max, option.value, option.value, 0, "",
                "", "server_config_change_cb", self.name, "", "")


def wrap_socket(server, file_descriptor):
    # type: (MatrixServer, int) -> socket.socket
    sock = None  # type: socket.socket

    temp_socket = socket.fromfd(
        file_descriptor,
        socket.AF_INET,
        socket.SOCK_STREAM
    )

    # TODO explain why these type gymnastics are needed
    # pylint: disable=protected-access
    if isinstance(temp_socket, socket._socket.socket):
        # pylint: disable=no-member
        sock = socket._socketobject(_sock=temp_socket)
    else:
        sock = temp_socket

    try:
        ssl_socket = server.ssl_context.wrap_socket(
            sock,
            server_hostname=server.address)  # type: ssl.SSLSocket

        return ssl_socket
    # TODO add the other relevant exceptions
    except ssl.SSLError as error:
        server_buffer_prnt(server, str(error))
        return None


def handle_http_response(server, message):
    # type: (MatrixServer, MatrixMessage) -> None

    assert message.response

    status_code = message.response.status

    # TODO handle error responses
    # TODO handle try again response
    if status_code == 200:
        # TODO json.loads can fail
        response = json.loads(message.response.body, encoding='utf-8')
        matrix_handle_message(
            server,
            message.type,
            response,
            message.extra_data
        )
    else:
        server_buffer_prnt(
            server,
            "ERROR IN HTTP RESPONSE {status_code}".format(
                status_code=status_code))

        server_buffer_prnt(server, message.request.request)
        server_buffer_prnt(server, message.response.body)

    return


def strip_matrix_server(string):
    # type: (unicode) -> unicode
    return string.rsplit(":", 1)[0]


def matrix_create_room_buffer(server, room_id):
    # type: (MatrixServer, unicode) -> None
    buf = W.buffer_new(
        room_id,
        "room_input_cb",
        server.name,
        "room_close_cb",
        server.name
    )

    W.buffer_set(buf, "localvar_set_type", 'channel')
    W.buffer_set(buf, "type", 'formated')
    W.buffer_set(buf, "localvar_set_channel", room_id)

    W.buffer_set(buf, "localvar_set_nick", server.user)

    W.buffer_set(buf, "localvar_set_server", server.name)

    short_name = strip_matrix_server(room_id)
    W.buffer_set(buf, "short_name", short_name)

    W.nicklist_add_group(
        buf,
        '',
        NICK_GROUP_HERE,
        "weechat.color.nicklist_group",
        1
    )

    W.buffer_set(buf, "nicklist", "1")
    W.buffer_set(buf, "nicklist_display_groups", "0")

    server.buffers[room_id] = buf
    server.rooms[room_id] = MatrixRoom(room_id)


def matrix_handle_room_aliases(server, room_id, event):
    # type: (MatrixServer, unicode, Dict[unicode, Any]) -> None
    buf = server.buffers[room_id]
    room = server.rooms[room_id]

    alias = event['content']['aliases'][-1]

    if not alias:
        return

    short_name = strip_matrix_server(alias)

    room.alias = alias
    W.buffer_set(buf, "name", alias)
    W.buffer_set(buf, "short_name", short_name)
    W.buffer_set(buf, "localvar_set_channel", alias)


def matrix_handle_room_members(server, room_id, event):
    # type: (MatrixServer, unicode, Dict[unicode, Any]) -> None
    buf = server.buffers[room_id]
    here = W.nicklist_search_group(buf, '', NICK_GROUP_HERE)

    # TODO print out a informational message
    if event['membership'] == 'join':
        # TODO set the buffer type to a channel if we have more than 2 users
        # TODO do we wan't to use the displayname here?
        nick = event['content']['displayname']
        nick_pointer = W.nicklist_search_nick(buf, "", nick)
        if not nick_pointer:
            W.nicklist_add_nick(buf, here, nick, "", "", "", 1)

    elif event['membership'] == 'leave':
        nick = event['content']['displayname']
        nick_pointer = W.nicklist_search_nick(buf, "", nick)
        if nick_pointer:
            W.nicklist_remove_nick(buf, nick_pointer)


def date_from_age(age):
    # type: (float) -> int
    now = time.time()
    date = int(now - (age / 1000))
    return date


def matrix_handle_room_text_message(server, room_id, event, old=False):
    # type: (MatrixServer, unicode, Dict[unicode, Any], bool) -> None
    tag = ""
    msg = event['content']['body']

    msg_author = strip_matrix_server(event['sender'])[1:]

    data = "{author}\t{msg}".format(author=msg_author, msg=msg)

    event_id = event['event_id']

    msg_date = date_from_age(event['unsigned']['age'])

    # TODO if this is an initial sync tag the messages as backlog
    if old:
        tag = ("nick_{a},matrix_id_{event_id},"
               "matrix_message,notify_message,no_log,no_highlight").format(
                   a=msg_author,
                   event_id=event_id)
    else:
        tag = ("nick_{a},matrix_id_{event_id},"
               "matrix_message,notify_message,log1").format(
                   a=msg_author,
                   event_id=event_id)

    buf = server.buffers[room_id]
    W.prnt_date_tags(buf, msg_date, tag, data)


def matrix_handle_redacted_message(server, room_id, event):
    # type: (MatrixServer, unicode, Dict[Any, Any]) -> None
    reason = ""
    censor = strip_matrix_server(
        event['unsigned']['redacted_because']['sender']
    )[1:]

    if 'reason' in event['unsigned']['redacted_because']['content']:
        reason = ", reason: \"{reason}\"".format(
            reason=event['unsigned']['redacted_because']['content']['reason'])

    msg = ("{del_color}<{log_color}Message redacted by: "
           "{censor}{log_color}{reason}{del_color}>{ncolor}").format(
               del_color=W.color("chat_delimiters"),
               ncolor=W.color("reset"),
               log_color=W.color("logger.color.backlog_line"),
               censor=censor,
               reason=reason)

    msg_author = strip_matrix_server(event['sender'])[1:]

    data = "{author}\t{msg}".format(author=msg_author, msg=msg)

    event_id = event['event_id']

    msg_date = date_from_age(event['unsigned']['age'])

    tag = ("nick_{a},matrix_id_{event_id},matrix_message,matrix_redacted,"
           "notify_message").format(a=msg_author, event_id=event_id)

    buf = server.buffers[room_id]
    W.prnt_date_tags(buf, msg_date, tag, data)


def matrix_handle_room_messages(server, room_id, event, old=False):
    # type: (MatrixServer, unicode, Dict[unicode, Any], bool) -> None
    if event['type'] == 'm.room.message':
        if 'redacted_by' in event['unsigned']:
            matrix_handle_redacted_message(server, room_id, event)
            return

        if event['content']['msgtype'] == 'm.text':
            matrix_handle_room_text_message(server, room_id, event, old)

        # TODO handle different content types here
        else:
            message = ("{prefix}Handling of content type "
                       "{type} not implemented").format(
                           type=event['content']['msgtype'],
                           prefix=W.prefix("error"))
            W.prnt(server.server_buffer, message)


def matrix_handle_room_events(server, room_id, room_events):
    # type: (MatrixServer, unicode, Dict[Any, Any]) -> None
    for event in room_events:
        if event['event_id'] in server.ignore_event_list:
            server.ignore_event_list.remove(event['event_id'])
            continue

        if event['type'] == 'm.room.aliases':
            matrix_handle_room_aliases(server, room_id, event)

        elif event['type'] == 'm.room.member':
            matrix_handle_room_members(server, room_id, event)

        elif event['type'] == 'm.room.message':
            matrix_handle_room_messages(server, room_id, event)

        elif event['type'] == 'm.room.topic':
            buf = server.buffers[room_id]
            room = server.rooms[room_id]
            topic = event['content']['topic']

            room.topic = topic
            room.topic_author = event['sender']

            topic_age = event['unsigned']['age']
            # TODO put the age calculation in a function
            room.topic_date = datetime.datetime.fromtimestamp(
                time.time() - (topic_age / 1000))

            W.buffer_set(buf, "title", topic)

            # TODO nick and topic color
            # TODO print old topic if configured so
            # TODO nick display name if configured so and found
            message = ("{prefix}{nick} has changed the topic for {room} to "
                       "\"{topic}\"").format(
                           prefix=W.prefix("network"),
                           nick=room.topic_author,
                           room=room.alias,
                           topic=topic)

            tags = "matrix_topic,log3"
            date = date_from_age(topic_age)

            W.prnt_date_tags(buf, date, tags, message)

        elif event['type'] == "m.room.redaction":
            pass

        else:
            message = ("{prefix}Handling of room event type "
                       "{type} not implemented").format(
                           type=event['type'],
                           prefix=W.prefix("error"))
            W.prnt(server.server_buffer, message)


def matrix_handle_room_info(server, room_info):
    # type: (MatrixServer, Dict) -> None
    for room_id, room in room_info['join'].iteritems():
        if not room_id:
            continue

        if room_id not in server.buffers:
            matrix_create_room_buffer(server, room_id)

        if not server.rooms[room_id].prev_batch:
            server.rooms[room_id].prev_batch = room['timeline']['prev_batch']

        matrix_handle_room_events(server, room_id, room['state']['events'])
        matrix_handle_room_events(server, room_id, room['timeline']['events'])


def matrix_sort_old_messages(server, room_id):
    lines = []
    buf = server.buffers[room_id]

    own_lines = W.hdata_pointer(W.hdata_get('buffer'), buf, 'own_lines')

    if own_lines:
        hdata_line = W.hdata_get('line')
        hdata_line_data = W.hdata_get('line_data')
        line = W.hdata_pointer(
            W.hdata_get('lines'),
            own_lines,
            'first_line'
        )

        while line:
            data = W.hdata_pointer(hdata_line, line, 'data')

            line_data = {}

            if data:
                date = W.hdata_time(hdata_line_data, data, 'date')
                print_date = W.hdata_time(hdata_line_data, data,
                                          'date_printed')
                tags = tags_from_line_data(data)
                prefix = W.hdata_string(hdata_line_data, data, 'prefix')
                message = W.hdata_string(hdata_line_data, data,
                                         'message')

                line_data = {'date': date,
                             'date_printed': print_date,
                             'tags_array': ','.join(tags),
                             'prefix': prefix,
                             'message': message}

                lines.append(line_data)

            line = W.hdata_move(hdata_line, line, 1)

        sorted_lines = sorted(lines, key=itemgetter('date'))
        lines = []

        # We need to convert the dates to a string for hdata_update(), this
        # will reverse the list at the same time
        while sorted_lines:
            line = sorted_lines.pop()
            new_line = {k: unicode(v) for k, v in line.items()}
            lines.append(new_line)

        matrix_update_buffer_lines(lines, own_lines)


def matrix_update_buffer_lines(new_lines, own_lines):
    hdata_line = W.hdata_get('line')
    hdata_line_data = W.hdata_get('line_data')

    line = W.hdata_pointer(
        W.hdata_get('lines'),
        own_lines,
        'first_line'
    )

    while line:
        data = W.hdata_pointer(hdata_line, line, 'data')

        if data:
            W.hdata_update(hdata_line_data, data, new_lines.pop())

        line = W.hdata_move(hdata_line, line, 1)


def matrix_handle_old_messages(server, room_id, events):
    for event in events:
        if event['type'] == 'm.room.message':
            matrix_handle_room_messages(server, room_id, event, old=True)
        # TODO do we wan't to handle topics joins/quits here?
        else:
            pass

    matrix_sort_old_messages(server, room_id)

    # matrix_handle_room_events(server, room_id, events)
    # TODO sort messages


def matrix_handle_message(
        server,        # type: MatrixServer
        message_type,  # type: MessageType
        response,      # type: Dict[unicode, Any]
        extra_data     # type: Dict[unicode, Any]
):
    # type: (...) -> None

    if message_type is MessageType.LOGIN:
        server.access_token = response["access_token"]
        message = MatrixMessage(server, MessageType.SYNC)
        send_or_queue(server, message)

    elif message_type is MessageType.SYNC:
        next_batch = response['next_batch']

        # we got the same batch again, nothing to do
        if next_batch == server.next_batch:
            return

        room_info = response['rooms']
        matrix_handle_room_info(server, room_info)

        server.next_batch = next_batch

    elif message_type is MessageType.SEND:
        author   = extra_data["author"]
        message  = extra_data["message"]
        room_id  = extra_data["room_id"]
        date     = int(time.time())
        event_id = response["event_id"]

        # This message will be part of the next sync, we already printed it out
        # so ignore it in the sync.
        server.ignore_event_list.append(event_id)

        tag = "nick_{a},matrix_id_{event_id},matrix_message".format(
            a=author, event_id=event_id)

        data = "{author}\t{msg}".format(author=author, msg=message)

        buf = server.buffers[room_id]
        W.prnt_date_tags(buf, date, tag, data)

    elif message_type == MessageType.ROOM_MSG:
        # Response has no messages, that is we already got the oldest message
        # in a previous request, nothing to do
        if not response['chunk']:
            return

        room_id = response['chunk'][0]['room_id']
        room = server.rooms[room_id]

        matrix_handle_old_messages(server, room_id, response['chunk'])

        room.prev_batch = response['end']

    # Nothing to do here, we'll handle state changes and redactions in the sync
    elif (message_type == MessageType.STATE or
          message_type == MessageType.REDACT):
        pass

    else:
        server_buffer_prnt(
            server,
            "Handling of message type {type} not implemented".format(
                type=message_type))


def matrix_login(server):
    # type: (MatrixServer) -> None
    post_data = {"type": "m.login.password",
                 "user": server.user,
                 "password": server.password}

    message = MatrixMessage(
        server,
        MessageType.LOGIN,
        data=post_data
    )
    send_or_queue(server, message)


def send_or_queue(server, message):
    # type: (MatrixServer, MatrixMessage) -> None
    if not send(server, message):
        server.send_queue.append(message)


def send(server, message):
    # type: (MatrixServer, MatrixMessage) -> bool

    request = message.request.request
    payload = message.request.payload

    try:
        server.socket.sendall(bytes(request, 'utf-8'))
        if payload:
            server.socket.sendall(bytes(payload, 'utf-8'))

        server.receive_queue.append(message)
        return True

    except socket.error as error:
        disconnect(server)
        server_buffer_prnt(server, str(error))
        return False


@utf8_decode
def receive_cb(server_name, file_descriptor):
    server = SERVERS[server_name]

    if not server.connected:
        server_buffer_prnt(server, "NOT CONNECTED WHILE RECEIVING")
        # can this happen?
        # do reconnection

    while True:
        try:
            data = server.socket.recv(4096)
        # TODO add the other relevant exceptions
        except ssl.SSLWantReadError:
            break
        except socket.error as error:
            disconnect(server)

            # Queue the failed message for resending
            message = server.receive_queue.popleft()
            server.send_queue.appendleft(message)

            server_buffer_prnt(server, error)
            return W.WEECHAT_RC_OK

        if not data:
            server_buffer_prnt(server, "No data while reading")
            disconnect(server)
            break

        received = len(data)  # type: int
        parsed_bytes = server.http_parser.execute(data, received)

        assert parsed_bytes == received

        if server.http_parser.is_partial_body():
            server.http_buffer.append(server.http_parser.recv_body())

        if server.http_parser.is_message_complete():
            status = server.http_parser.get_status_code()
            headers = server.http_parser.get_headers()
            body = b"".join(server.http_buffer)

            message = server.receive_queue.popleft()
            message.response = HttpResponse(status, headers, body)

            # Message done, reset the parser state.
            server.http_parser = HttpParser()
            server.http_buffer = []

            handle_http_response(server, message)
            break

    return W.WEECHAT_RC_OK


def disconnect(server):
    # type: (MatrixServer) -> None
    if server.fd_hook:
        W.unhook(server.fd_hook)

    server.fd_hook    = None
    server.socket     = None
    server.connected  = False

    server_buffer_prnt(server, "Disconnected")


def server_buffer_prnt(server, string):
    # type: (MatrixServer, unicode) -> None
    assert server.server_buffer
    buffer = server.server_buffer
    now = int(time.time())
    W.prnt_date_tags(buffer, now, "", string)


def server_buffer_set_title(server):
    # type: (MatrixServer) -> None
    if server.numeric_address:
        ip_string = " ({address})".format(address=server.numeric_address)
    else:
        ip_string = ""

    title = ("Matrix: {address}/{port}{ip}").format(
        address=server.address,
        port=server.port,
        ip=ip_string)

    W.buffer_set(server.server_buffer, "title", title)


def create_server_buffer(server):
    # type: (MatrixServer) -> None
    server.server_buffer = W.buffer_new(
        server.name,
        "server_buffer_cb",
        server.name,
        "",
        ""
    )

    server_buffer_set_title(server)
    W.buffer_set(server.server_buffer, "localvar_set_type", 'server')
    W.buffer_set(server.server_buffer, "localvar_set_nick", server.user)
    W.buffer_set(server.server_buffer, "localvar_set_server", server.name)
    W.buffer_set(server.server_buffer, "localvar_set_channel", server.name)

    # TODO merge without core
    if GLOBAL_OPTIONS.look_server_buf == ServerBufferType.MERGE_CORE:
        W.buffer_merge(server.server_buffer, W.buffer_search_main())
    elif GLOBAL_OPTIONS.look_server_buf == ServerBufferType.MERGE:
        pass
    else:
        pass


# TODO if we're reconnecting we should retry even if there was an error on the
# socket creation
@utf8_decode
def connect_cb(data, status, gnutls_rc, sock, error, ip_address):
    # pylint: disable=too-many-arguments,too-many-branches
    status_value = int(status)  # type: int
    server = SERVERS[data]

    if status_value == W.WEECHAT_HOOK_CONNECT_OK:
        file_descriptor = int(sock)  # type: int
        sock = wrap_socket(server, file_descriptor)

        if sock:
            server.socket = sock
            hook = W.hook_fd(
                server.socket.fileno(),
                1, 0, 0,
                "receive_cb",
                server.name
            )

            server.fd_hook         = hook
            server.connected       = True
            server.connecting      = False
            server.reconnect_count = 0
            server.numeric_address = ip_address

            server_buffer_set_title(server)
            server_buffer_prnt(server, "Connected")

            if not server.access_token:
                matrix_login(server)
        else:
            reconnect(server)

    elif status_value == W.WEECHAT_HOOK_CONNECT_ADDRESS_NOT_FOUND:
        W.prnt("", '{address} not found'.format(address=ip_address))

    elif status_value == W.WEECHAT_HOOK_CONNECT_IP_ADDRESS_NOT_FOUND:
        W.prnt("", 'IP address not found')

    elif status_value == W.WEECHAT_HOOK_CONNECT_CONNECTION_REFUSED:
        W.prnt("", 'Connection refused')

    elif status_value == W.WEECHAT_HOOK_CONNECT_PROXY_ERROR:
        W.prnt("", 'Proxy fails to establish connection to server')

    elif status_value == W.WEECHAT_HOOK_CONNECT_LOCAL_HOSTNAME_ERROR:
        W.prnt("", 'Unable to set local hostname')

    elif status_value == W.WEECHAT_HOOK_CONNECT_GNUTLS_INIT_ERROR:
        W.prnt("", 'TLS init error')

    elif status_value == W.WEECHAT_HOOK_CONNECT_GNUTLS_HANDSHAKE_ERROR:
        W.prnt("", 'TLS Handshake failed')

    elif status_value == W.WEECHAT_HOOK_CONNECT_MEMORY_ERROR:
        W.prnt("", 'Not enough memory')

    elif status_value == W.WEECHAT_HOOK_CONNECT_TIMEOUT:
        W.prnt("", 'Timeout')

    elif status_value == W.WEECHAT_HOOK_CONNECT_SOCKET_ERROR:
        W.prnt("", 'Unable to create socket')
    else:
        W.prnt("", 'Unexpected error: {status}'.format(status=status_value))

    return W.WEECHAT_RC_OK


def reconnect(server):
    # type: (MatrixServer) -> None
    # TODO this needs some more work, do we want a reconnecting flag?
    server.connecting = True
    timeout = server.reconnect_count * 5 * 1000

    if timeout > 0:
        server_buffer_prnt(
            server,
            "Reconnecting in {timeout} seconds.".format(
                timeout=timeout / 1000))
        W.hook_timer(timeout, 0, 1, "reconnect_cb", server.name)
    else:
        connect(server)

    server.reconnect_count += 1


@utf8_decode
def reconnect_cb(server_name, remaining):
    server = SERVERS[server_name]
    connect(server)

    return W.WEECHAT_RC_OK


def connect(server):
    # type: (MatrixServer) -> bool
    if not server.address or not server.port:
        message = "{prefix}Server address or port not set".format(
            prefix=W.prefix("error"))
        W.prnt("", message)
        return False

    if not server.user or not server.password:
        message = "{prefix}User or password not set".format(
            prefix=W.prefix("error"))
        W.prnt("", message)
        return False

    if server.connected:
        return True

    if not server.server_buffer:
        create_server_buffer(server)

    if not server.timer_hook:
        server.timer_hook = W.hook_timer(
            1 * 1000,
            0,
            0,
            "matrix_timer_cb",
            server.name
        )

    W.hook_connect("", server.address, server.port, 1, 0, "",
                   "connect_cb", server.name)

    return W.WEECHAT_RC_OK


@utf8_decode
def room_input_cb(server_name, buffer, input_data):
    server = SERVERS[server_name]

    if not server.connected:
        message = "{prefix}you are not connected to the server".format(
            prefix=W.prefix("error"))
        W.prnt(buffer, message)
        return W.WEECHAT_RC_ERROR

    room_id = key_from_value(server.buffers, buffer)
    body = {"msgtype": "m.text", "body": input_data}
    extra_data = {
        "author": server.user,
        "message": input_data,
        "room_id": room_id
    }

    message = MatrixMessage(server, MessageType.SEND,
                            data=body, room_id=room_id,
                            extra_data=extra_data)

    send_or_queue(server, message)
    return W.WEECHAT_RC_OK


@utf8_decode
def room_close_cb(data, buffer):
    W.prnt("", "Buffer '%s' will be closed!" %
           W.buffer_get_string(buffer, "name"))
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_timer_cb(server_name, remaining_calls):
    server = SERVERS[server_name]

    if not server.connected:
        if not server.connecting:
            server_buffer_prnt(server, "Reconnecting timeout blaaaa")
            reconnect(server)
        return W.WEECHAT_RC_OK

    while server.send_queue:
        message = server.send_queue.popleft()

        if not send(server, message):
            # We got an error while sending the last message return the message
            # to the queue and exit the loop
            server.send_queue.appendleft(message)
            break

    for message in server.message_queue:
        server_buffer_prnt(
            server,
            "Handling message: {message}".format(message=message))

    # TODO don't send this out here, if a SYNC fails for some reason (504 try
    # again!) we'll hammer the server unnecessarily, send it out after a
    # successful sync or after a 504 sync with a proper timeout
    if server.next_batch:
        message = MatrixMessage(server, MessageType.SYNC)
        server.send_queue.append(message)

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_config_reload_cb(data, config_file):
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_config_server_read_cb(
        data, config_file, section,
        option_name, value
):

    return_code = W.WEECHAT_CONFIG_OPTION_SET_ERROR

    if option_name:
        server_name, option = option_name.rsplit('.', 1)
        server = None

        if server_name in SERVERS:
            server = SERVERS[server_name]
        else:
            server = MatrixServer(server_name, config_file)
            SERVERS[server.name] = server

        # Ignore invalid options
        if option in server.options:
            return_code = W.config_option_set(server.options[option], value, 1)

    # TODO print out error message in case of erroneous return_code

    return return_code


@utf8_decode
def matrix_config_server_write_cb(data, config_file, section_name):
    if not W.config_write_line(config_file, section_name, ""):
        return W.WECHAT_CONFIG_WRITE_ERROR

    for server in SERVERS.values():
        for option in server.options.values():
            if not W.config_write_option(config_file, option):
                return W.WECHAT_CONFIG_WRITE_ERROR

    return W.WEECHAT_CONFIG_WRITE_OK


@utf8_decode
def matrix_config_change_cb(data, option):
    option_name = key_from_value(GLOBAL_OPTIONS.options, option)

    if option_name == "redactions":
        GLOBAL_OPTIONS.redaction_type = W.config_integer(option)
    elif option_name == "server_buffer":
        GLOBAL_OPTIONS.look_server_buf = W.config_integer(option)

    return 1


def init_matrix_config():
    config_file = W.config_new("matrix", "matrix_config_reload_cb", "")

    look_options = [
        Option(
            "redactions", "integer",
            "strikethrough|notice|delete", 0, 0,
            "strikethrough",
            (
                "only notice redactions, strike through or delete "
                "redacted messages"
            )
        ),
        Option(
            "server_buffer", "integer",
            "merge_with_core|merge_without_core|independent",
            0, 0, "merge_with_core", "merge server buffers"
        )
    ]

    def add_global_options(section, options):
        for option in options:
            GLOBAL_OPTIONS.options[option.name] = W.config_new_option(
                config_file, section, option.name,
                option.type, option.description, option.string_values,
                option.min, option.max, option.value, option.value, 0, "",
                "", "matrix_config_change_cb", "", "", "")

    section = W.config_new_section(config_file, "color", 0, 0, "", "", "", "",
                                   "", "", "", "", "", "")

    # TODO color options

    section = W.config_new_section(config_file, "look", 0, 0, "", "", "", "",
                                   "", "", "", "", "", "")

    add_global_options(section, look_options)

    section = W.config_new_section(config_file, "network", 0, 0, "", "", "",
                                   "", "", "", "", "", "", "")

    # TODO network options

    W.config_new_section(
        config_file, "server",
        0, 0,
        "matrix_config_server_read_cb",
        "",
        "matrix_config_server_write_cb",
        "", "", "", "", "", "", ""
    )

    return config_file


def read_matrix_config():
    # type: () -> bool
    return_code = W.config_read(CONFIG)
    if return_code == weechat.WEECHAT_CONFIG_READ_OK:
        return True
    elif return_code == weechat.WEECHAT_CONFIG_READ_MEMORY_ERROR:
        return False
    elif return_code == weechat.WEECHAT_CONFIG_READ_FILE_NOT_FOUND:
        return True
    return False


@utf8_decode
def matrix_unload_cb():
    for section in ["network", "look", "color", "server"]:
        section_pointer = W.config_search_section(CONFIG, section)
        W.config_section_free_options(section_pointer)
        W.config_section_free(section_pointer)

    W.config_free(CONFIG)

    return W.WEECHAT_RC_OK


def check_server_existence(server_name, servers):
    if server_name not in servers:
        message = "{prefix}matrix: No such server: {server} found".format(
            prefix=W.prefix("error"), server=server_name)
        W.prnt("", message)
        return False
    return True


def matrix_command_help(args):
    if not args:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix help\" (see the help for the command: "
                   "/matrix help help").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return

    for command in args:
        message = ""

        if command == "connect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/connect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "connect to Matrix server(s)"
                       "\n\n"
                       "server-name: server to connect to"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "disconnect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/disconnect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "disconnect from Matrix server(s)"
                       "\n\n"
                       "server-name: server to disconnect"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "reconnect":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/reconnect{ncolor} "
                       "<server-name> [<server-name>...]"
                       "\n\n"
                       "reconnect to Matrix server(s)"
                       "\n\n"
                       "server-name: server to reconnect"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "server":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/server{ncolor} "
                       "add <server-name> <hostname>[:<port>]"
                       "\n                  "
                       "delete|list|listfull <server-name>"
                       "\n\n"
                       "list, add, or remove Matrix servers"
                       "\n\n"
                       "       list: list servers (without argument, this "
                       "list is displayed)\n"
                       "   listfull: list servers with detailed info for each "
                       "server\n"
                       "        add: add a new server\n"
                       "     delete: delete a server\n"
                       "server-name: server to reconnect (internal name)\n"
                       "   hostname: name or IP address of server\n"
                       "       port: port of server (default: 8448)\n"
                       "\n"
                       "Examples:"
                       "\n  /server listfull"
                       "\n  /server add matrix matrix.org:80"
                       "\n  /server del matrix").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        elif command == "help":
            message = ("{delimiter_color}[{ncolor}matrix{delimiter_color}]  "
                       "{ncolor}{cmd_color}/help{ncolor} "
                       "<matrix-command> [<matrix-command>...]"
                       "\n\n"
                       "display help about Matrix commands"
                       "\n\n"
                       "matrix-command: a Matrix command name"
                       "(internal name)").format(
                           delimiter_color=W.color("chat_delimiters"),
                           cmd_color=W.color("chat_buffer"),
                           ncolor=W.color("reset"))

        else:
            message = ("{prefix}matrix: No help available, \"{command}\" "
                       "is not a matrix command").format(
                           prefix=W.prefix("error"),
                           command=command)

        W.prnt("", "")
        W.prnt("", message)

        return


def matrix_server_command_listfull(args):
    def get_value_string(value, default_value):
        if value == default_value:
            if not value:
                value = "''"
            value_string = "  ({value})".format(value=value)
        else:
            value_string = "{color}{value}{ncolor}".format(
                color=W.color("chat_value"),
                value=value,
                ncolor=W.color("reset"))

        return value_string

    for server_name in args:
        if server_name not in SERVERS:
            continue

        server = SERVERS[server_name]
        connected = ""

        W.prnt("", "")

        if server.connected:
            connected = "connected"
        else:
            connected = "not connected"

        message = ("Server: {server_color}{server}{delimiter_color}"
                   " [{ncolor}{connected}{delimiter_color}]"
                   "{ncolor}").format(
                       server_color=W.color("chat_server"),
                       server=server.name,
                       delimiter_color=W.color("chat_delimiters"),
                       connected=connected,
                       ncolor=W.color("reset"))

        W.prnt("", message)

        option = server.options["autoconnect"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  autoconnect. : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["address"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  address. . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["port"]
        default_value = str(W.config_integer_default(option))
        value = str(W.config_integer(option))

        value_string = get_value_string(value, default_value)
        message = "  port . . . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["username"]
        default_value = W.config_string_default(option)
        value = W.config_string(option)

        value_string = get_value_string(value, default_value)
        message = "  username . . : {value}".format(value=value_string)

        W.prnt("", message)

        option = server.options["password"]
        value = W.config_string(option)

        if value:
            value = "(hidden)"

        value_string = get_value_string(value, '')
        message = "  password . . : {value}".format(value=value_string)

        W.prnt("", message)


def matrix_server_command_delete(args):
    for server_name in args:
        if check_server_existence(server_name, SERVERS):
            server = SERVERS[server_name]

            if server.connected:
                message = ("{prefix}matrix: you can not delete server "
                           "{color}{server}{ncolor} because you are "
                           "connected to it. Try \"/matrix disconnect "
                           "{color}{server}{ncolor}\" before.").format(
                               prefix=W.prefix("error"),
                               color=W.color("chat_server"),
                               ncolor=W.color("reset"),
                               server=server.name)
                W.prnt("", message)
                return

            for buf in server.buffers.values():
                W.buffer_close(buf)

            if server.server_buffer:
                W.buffer_close(server.server_buffer)

            for option in server.options.values():
                W.config_option_free(option)

            message = ("matrix: server {color}{server}{ncolor} has been "
                       "deleted").format(
                           server=server.name,
                           color=W.color("chat_server"),
                           ncolor=W.color("reset"))

            del SERVERS[server.name]
            server = None

            W.prnt("", message)


def matrix_server_command_add(args):
    if len(args) < 2:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix server add\" (see the help for the command: "
                   "/matrix help server").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return
    elif len(args) > 4:
        message = ("{prefix}matrix: Too many arguments for command "
                   "\"/matrix server add\" (see the help for the command: "
                   "/matrix help server").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return

    def remove_server(server):
        for option in server.options.values():
            W.config_option_free(option)
        del SERVERS[server.name]

    server_name = args[0]

    if server_name in SERVERS:
        message = ("{prefix}matrix: server {color}{server}{ncolor} "
                   "already exists, can't add it").format(
                       prefix=W.prefix("error"),
                       color=W.color("chat_server"),
                       server=server_name,
                       ncolor=W.color("reset"))
        W.prnt("", message)
        return

    server = MatrixServer(args[0], CONFIG)
    SERVERS[server.name] = server

    if len(args) >= 2:
        try:
            host, port = args[1].split(":", 1)
        except ValueError:
            host, port = args[1], None

        return_code = W.config_option_set(
            server.options["address"],
            host,
            1
        )

        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set address for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))

            W.prnt("", message)
            server = None
            return

        if port:
            return_code = W.config_option_set(
                server.options["port"],
                port,
                1
            )
            if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
                remove_server(server)
                message = ("{prefix}Failed to set port for server "
                           "{color}{server}{ncolor}, failed to add "
                           "server.").format(
                               prefix=W.prefix("error"),
                               color=W.color("chat_server"),
                               server=server.name,
                               ncolor=W.color("reset"))

                W.prnt("", message)
                server = None
                return

    if len(args) >= 3:
        user = args[2]
        return_code = W.config_option_set(
            server.options["username"],
            user,
            1
        )

        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set user for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))

            W.prnt("", message)
            server = None
            return

    if len(args) == 4:
        password = args[3]

        return_code = W.config_option_set(
            server.options["password"],
            password,
            1
        )
        if return_code == W.WEECHAT_CONFIG_OPTION_SET_ERROR:
            remove_server(server)
            message = ("{prefix}Failed to set password for server "
                       "{color}{server}{ncolor}, failed to add "
                       "server.").format(
                           prefix=W.prefix("error"),
                           color=W.color("chat_server"),
                           server=server.name,
                           ncolor=W.color("reset"))
            W.prnt("", message)
            server = None
            return

    message = ("matrix: server {color}{server}{ncolor} "
               "has been added").format(
                   server=server.name,
                   color=W.color("chat_server"),
                   ncolor=W.color("reset"))
    W.prnt("", message)


def matrix_server_command(command, args):
    def list_servers(_):
        if SERVERS:
            W.prnt("", "\nAll matrix servers:")
            for server in SERVERS:
                W.prnt("", "    {color}{server}".format(
                    color=W.color("chat_server"),
                    server=server
                ))

    # TODO the argument for list and listfull is used as a match word to
    # find/filter servers, we're currently match exactly to the whole name
    if command == 'list':
        list_servers(args)
    elif command == 'listfull':
        matrix_server_command_listfull(args)
    elif command == 'add':
        matrix_server_command_add(args)
    elif command == 'delete':
        matrix_server_command_delete(args)
    else:
        message = ("{prefix}matrix: Error: unknown matrix server command, "
                   "\"{command}\" (type /matrix help server for help)").format(
                       prefix=W.prefix("error"),
                       command=command)
        W.prnt("", message)


@utf8_decode
def matrix_command_cb(data, buffer, args):
    def connect_server(args):
        for server_name in args:
            if check_server_existence(server_name, SERVERS):
                server = SERVERS[server_name]
                connect(server)

    def disconnect_server(args):
        for server_name in args:
            if check_server_existence(server_name, SERVERS):
                server = SERVERS[server_name]
                W.unhook(server.timer_hook)
                server.timer_hook = None
                disconnect(server)

    split_args = list(filter(bool, args.split(' ')))

    if len(split_args) < 1:
        message = ("{prefix}matrix: Too few arguments for command "
                   "\"/matrix\" (see the help for the command: "
                   "/help matrix").format(prefix=W.prefix("error"))
        W.prnt("", message)
        return W.WEECHAT_RC_ERROR

    command, args = split_args[0], split_args[1:]

    if command == 'connect':
        connect_server(args)

    elif command == 'disconnect':
        disconnect_server(args)

    elif command == 'reconnect':
        disconnect_server(args)
        connect_server(args)

    elif command == 'server':
        if len(args) >= 1:
            subcommand, args = args[0], args[1:]
            matrix_server_command(subcommand, args)
        else:
            matrix_server_command("list", "")

    elif command == 'help':
        matrix_command_help(args)

    else:
        message = ("{prefix}matrix: Error: unknown matrix command, "
                   "\"{command}\" (type /help matrix for help)").format(
                       prefix=W.prefix("error"),
                       command=command)
        W.prnt("", message)

    return W.WEECHAT_RC_OK


def add_servers_to_completion(completion):
    for server_name in SERVERS:
        W.hook_completion_list_add(
            completion,
            server_name,
            0,
            weechat.WEECHAT_LIST_POS_SORT
        )


@utf8_decode
def server_command_completion_cb(data, completion_item, buffer, completion):
    buffer_input = weechat.buffer_get_string(buffer, "input").split()

    args = buffer_input[1:]
    commands = ['add', 'delete', 'list', 'listfull']

    def complete_commands():
        for command in commands:
            W.hook_completion_list_add(
                completion,
                command,
                0,
                weechat.WEECHAT_LIST_POS_SORT
            )

    if len(args) == 1:
        complete_commands()

    elif len(args) == 2:
        if args[1] not in commands:
            complete_commands()
        else:
            if args[1] == 'delete' or args[1] == 'listfull':
                add_servers_to_completion(completion)

    elif len(args) == 3:
        if args[1] == 'delete' or args[1] == 'listfull':
            if args[2] not in SERVERS:
                add_servers_to_completion(completion)

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_server_completion_cb(data, completion_item, buffer, completion):
    add_servers_to_completion(completion)
    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_command_completion_cb(data, completion_item, buffer, completion):
    for command in ["connect", "disconnect", "reconnect", "server", "help"]:
        W.hook_completion_list_add(
            completion,
            command,
            0,
            weechat.WEECHAT_LIST_POS_SORT)
    return W.WEECHAT_RC_OK


def create_default_server(config_file):
    server = MatrixServer('matrix.org', config_file)
    SERVERS[server.name] = server

    # TODO set this to matrix.org
    W.config_option_set(server.options["address"], "localhost", 1)

    return True


@utf8_decode
def matrix_command_topic_cb(data, buffer, command):
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            topic = None
            room_id = key_from_value(server.buffers, buffer)
            split_command = command.split(' ', 1)

            if len(split_command) == 2:
                topic = split_command[1]

            if not topic:
                room = server.rooms[room_id]
                if not room.topic:
                    return W.WEECHAT_RC_OK

                message = ("{prefix}Topic for {color}{room}{ncolor} is "
                           "\"{topic}\"").format(
                               prefix=W.prefix("network"),
                               color=W.color("chat_buffer"),
                               ncolor=W.color("reset"),
                               room=room.alias,
                               topic=room.topic)

                date = int(time.time())
                topic_date = room.topic_date.strftime("%a, %d %b %Y "
                                                      "%H:%M:%S")

                tags = "matrix_topic,log1"
                W.prnt_date_tags(buffer, date, tags, message)

                # TODO the nick should be colored
                # TODO we should use the display name as well as
                # the user name here
                message = ("{prefix}Topic set by {author} on "
                           "{date}").format(
                               prefix=W.prefix("network"),
                               author=room.topic_author,
                               date=topic_date)
                W.prnt_date_tags(buffer, date, tags, message)

                return W.WEECHAT_RC_OK_EAT

            body = {"topic": topic}

            message = MatrixMessage(
                server,
                MessageType.STATE,
                data=body,
                room_id=room_id,
                extra_id="m.room.topic"
            )
            send_or_queue(server, message)

            return W.WEECHAT_RC_OK_EAT

        elif buffer == server.server_buffer:
            message = ("{prefix}matrix: command \"topic\" must be "
                       "executed on a Matrix channel buffer").format(
                           prefix=W.prefix("error"))
            W.prnt(buffer, message)
            return W.WEECHAT_RC_OK_EAT

    return W.WEECHAT_RC_OK


def matrix_fetch_old_messages(server, room_id):
    room = server.rooms[room_id]
    prev_batch = room.prev_batch

    if not prev_batch:
        return

    message = MatrixMessage(server, MessageType.ROOM_MSG,
                            room_id=room_id, extra_id=prev_batch)

    send_or_queue(server, message)

    return


@utf8_decode
def matrix_command_buf_clear_cb(data, buffer, command):
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            room_id = key_from_value(server.buffers, buffer)
            server.rooms[room_id].prev_batch = server.next_batch

            return W.WEECHAT_RC_OK

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_command_pgup_cb(data, buffer, command):
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            window = W.window_search_with_buffer(buffer)

            first_line_displayed = bool(
                W.window_get_integer(window, "first_line_displayed")
            )

            if first_line_displayed:
                room_id = key_from_value(server.buffers, buffer)
                matrix_fetch_old_messages(server, room_id)

            return W.WEECHAT_RC_OK

    return W.WEECHAT_RC_OK


def tags_from_line_data(line_data, ):
    # type: (weechat.hdata) -> List[unicode]
    tags_count = W.hdata_get_var_array_size(
        W.hdata_get('line_data'),
        line_data,
        'tags_array')

    tags = [
        W.hdata_string(
            W.hdata_get('line_data'),
            line_data,
            '%d|tags_array' % i
        ) for i in range(tags_count)]

    return tags


def event_id_from_line(buf, target_number):
    # type: (weechat.buffer, int) -> unicode
    own_lines = W.hdata_pointer(W.hdata_get('buffer'), buf, 'own_lines')
    if own_lines:
        line = W.hdata_pointer(
            W.hdata_get('lines'),
            own_lines,
            'last_line'
        )

        line_number = 1

        while line:
            line_data = W.hdata_pointer(
                W.hdata_get('line'),
                line,
                'data'
            )

            if line_data:
                tags = tags_from_line_data(line_data)

                # Only count non redacted user messages
                if ('matrix_message' in tags
                        and 'matrix_redacted' not in tags):

                    if line_number == target_number:
                        for tag in tags:
                            if tag.startswith("matrix_id"):
                                event_id = tag[10:]
                                return event_id

                    line_number += 1

            line = W.hdata_move(W.hdata_get('line'), line, -1)

    return ""


@utf8_decode
def matrix_redact_command_cb(data, buffer, args):
    for server in SERVERS.values():
        if buffer in server.buffers.values():
            body = {}

            room_id = key_from_value(server.buffers, buffer)

            matches = re.match(r"(\d+)(:\".*\")? ?(.*)?", args)

            if not matches:
                message = ("{prefix}matrix: Invalid command arguments (see "
                           "the help for the command /help redact)").format(
                               prefix=W.prefix("error"))
                W.prnt("", message)
                return W.WEECHAT_RC_ERROR

            line_string, _, reason = matches.groups()
            line = int(line_string)

            if reason:
                body = {"reason": reason}

            event_id = event_id_from_line(buffer, line)

            if not event_id:
                message = ("{prefix}matrix: No such message with number "
                           "{number} found").format(
                               prefix=W.prefix("error"),
                               number=line)
                W.prnt("", message)
                return W.WEECHAT_RC_OK

            message = MatrixMessage(
                server,
                MessageType.REDACT,
                data=body,
                room_id=room_id,
                extra_id=event_id
            )
            send_or_queue(server, message)

            return W.WEECHAT_RC_OK

        elif buffer == server.server_buffer:
            message = ("{prefix}matrix: command \"redact\" must be "
                       "executed on a Matrix channel buffer").format(
                           prefix=W.prefix("error"))
            W.prnt("", message)
            return W.WEECHAT_RC_OK

    return W.WEECHAT_RC_OK


@utf8_decode
def matrix_message_completion_cb(data, completion_item, buffer, completion):
    own_lines = W.hdata_pointer(W.hdata_get('buffer'), buffer, 'own_lines')
    if own_lines:
        line = W.hdata_pointer(
            W.hdata_get('lines'),
            own_lines,
            'last_line'
        )

        line_number = 1

        while line:
            line_data = W.hdata_pointer(
                W.hdata_get('line'),
                line,
                'data'
            )

            if line_data:
                message = W.hdata_string(W.hdata_get('line_data'), line_data,
                                         'message')

                tags = tags_from_line_data(line_data)

                # Only add non redacted user messages to the completion
                if (message
                        and 'matrix_message' in tags
                        and 'matrix_redacted' not in tags):

                    if len(message) > GLOBAL_OPTIONS.redaction_comp_len + 2:
                        message = (
                            message[:GLOBAL_OPTIONS.redaction_comp_len]
                            + '..')

                    item = ("{number}:\"{message}\"").format(
                        number=line_number,
                        message=message)

                    W.hook_completion_list_add(
                        completion,
                        item,
                        0,
                        weechat.WEECHAT_LIST_POS_END)
                    line_number += 1

            line = W.hdata_move(W.hdata_get('line'), line, -1)

    return W.WEECHAT_RC_OK


def init_hooks():
    W.hook_completion(
        "matrix_server_commands",
        "Matrix server completion",
        "server_command_completion_cb",
        ""
    )

    W.hook_completion(
        "matrix_servers",
        "Matrix server completion",
        "matrix_server_completion_cb",
        ""
    )

    W.hook_completion(
        "matrix_commands",
        "Matrix command completion",
        "matrix_command_completion_cb",
        ""
    )

    W.hook_completion(
        "matrix_messages",
        "Matrix message completion",
        "matrix_message_completion_cb",
        ""
    )

    W.hook_command(
        # Command name and short description
        'matrix', 'Matrix chat protocol command',
        # Synopsis
        (
            'server add <server-name> <hostname>[:<port>] ||'
            'server delete|list|listfull <server-name> ||'
            'connect <server-name> ||'
            'disconnect <server-name> ||'
            'reconnect <server-name> ||'
            'help <matrix-command>'
        ),
        # Description
        (
            '    server: list, add, or remove Matrix servers\n'
            '   connect: connect to Matrix servers\n'
            'disconnect: disconnect from one or all Matrix servers\n'
            ' reconnect: reconnect to server(s)\n\n'
            '      help: show detailed command help\n\n'
            'Use /matrix help [command] to find out more\n'
        ),
        # Completions
        (
            'server %(matrix_server_commands)|%* ||'
            'connect %(matrix_servers) ||'
            'disconnect %(matrix_servers) ||'
            'reconnect %(matrix_servers) ||'
            'help %(matrix_commands)'
        ),
        # Function name
        'matrix_command_cb', '')

    W.hook_command(
        # Command name and short description
        'redact', 'redact messages',
        # Synopsis
        (
            '<message-number>[:<"message-part">] [<reason>]'
        ),
        # Description
        (
            "message-number: number of the message to redact (message numbers"
            "\n                start from the last recieved as "
            "1 and count up)\n"
            "  message-part: a shortened part of the message\n"
            "        reason: the redaction reason\n"
        ),
        # Completions
        (
            '%(matrix_messages)'
        ),
        # Function name
        'matrix_redact_command_cb', '')

    W.hook_command_run('/topic', 'matrix_command_topic_cb', '')
    W.hook_command_run('/window page_up', 'matrix_command_pgup_cb', '')
    W.hook_command_run('/buffer clear', 'matrix_command_buf_clear_cb', '')


def autoconnect(servers):
    for server in servers.values():
        if server.autoconnect:
            connect(server)


if __name__ == "__main__":
    W = WeechatWrapper(weechat)

    if W.register(WEECHAT_SCRIPT_NAME,
                  WEECHAT_SCRIPT_AUTHOR,
                  WEECHAT_SCRIPT_VERSION,
                  WEECHAT_SCRIPT_LICENSE,
                  WEECHAT_SCRIPT_DESCRIPTION,
                  'matrix_unload_cb',
                  ''):

        GLOBAL_OPTIONS = PluginOptions()

        # TODO if this fails we should abort and unload the script.
        CONFIG = init_matrix_config()
        read_matrix_config()

        init_hooks()

        if not SERVERS:
            create_default_server(CONFIG)

        autoconnect(SERVERS)
