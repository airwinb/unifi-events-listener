#!/usr/bin/env python3

# packages to install:
# pip install aiohttp
# pip install asyncio

# TODO
# - define event filter and url actions
# - call url when event occurs

from __future__ import print_function

import asyncio
import aiohttp
from configparser import ConfigParser
import json
import logging
import os
import requests
import sys
import time
import threading
from queue import Queue
import collections

# Constants (Do not change)
__version__ = '0.1.0-SNAPSHOT'
__date__ = '2019-11-03'
__updated__ = '2019-11-08'

# Global variables
logger = None
send_to_server = True


class UnifiClient(object):

    def __init__(self, username, password, host='localhost', port=8443, ssl_verify=False, timeout=10.0):
        self.username = username
        self.password = password
        self.host = host
        self.port = port
        self.ssl_verify = ssl_verify
        self.timeout = timeout
        
        self.url = 'https://' + self.host + ':' + str(port) + '/'
        self.login_url = self.url + 'api/login'
        self.initial_info_url = self.url + 'api/s/default/stat/device'
        self.params = {'_depth': 4, 'test': 0}
        self.ws_url = 'wss://{}:{}/wss/s/default/events'.format(self.host, self.port)
        
        # dictionary for storing unifi data
        self.unifi_data = collections.OrderedDict()

        self.event_q = Queue(10)

        logger.debug('Python: %s' % repr(sys.version_info))
        
        self.connect_websocket()
        
    def connect_websocket(self):
        t = threading.Thread(target=self.start_websocket)
        t.daemon = True
        t.start()

    def start_websocket(self):
        logger.info('Python 3 websocket')
        loop = asyncio.new_event_loop()
        while True:
            loop.run_until_complete(self.async_websocket())
            time.sleep(30)
            logger.warning('Reconnecting websocket')

    async def async_websocket(self):
        """
        By default ClientSession uses strict version of aiohttp.CookieJar. RFC 2109 explicitly forbids cookie
        accepting from URLs with IP address instead of DNS name (e.g. http://127.0.0.1:80/cookie).
        It’s good but sometimes for testing we need to enable support for such cookies. It should be done by
        passing unsafe=True to aiohttp.CookieJar constructor:
        """

        # enable support for unsafe cookies
        jar = aiohttp.CookieJar(unsafe=True)

        logger.info('login() %s as %s' % (self.url, self.username))

        json_request = {'username': self.username, 'password': self.password, 'strict': True}

        try:
            async with aiohttp.ClientSession(cookie_jar=jar) as session:
                async with session.post(
                        self.login_url, json=json_request, ssl=self.ssl_verify) as response:
                    assert response.status == 200
                    json_response = await response.json()
                    logger.debug('Received json response to login:')
                    logger.debug(json.dumps(json_response, indent=2))

                async with session.get(
                        self.initial_info_url, json=self.params, ssl=self.ssl_verify) as response:
                    assert response.status == 200
                    json_response = await response.json()
                    logger.debug('Received json response to initial data:')
                    logger.debug(json.dumps(json_response, indent=2))
                    self.update_unifi_data(json_response)

                async with session.ws_connect(self.ws_url, ssl=self.ssl_verify) as ws:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            # logger.debug('received: %s' % json.dumps(json.loads(msg.data), indent=2))
                            self.update_unifi_data(msg.json(loads=json.loads))
                        elif msg.type == aiohttp.WSMsgType.CLOSED:
                            logger.info('WS closed')
                            break
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            logger.error('WS closed with Error')
                            break

        except AssertionError as e:
            logger.error('failed to connect: %s' % e)

        logger.info('Exited')
        
    def update_unifi_data(self, data):
        """
        takes data from the websocket and puts events in the queue
        Uses OrderDict to preserve the order for repeatable output.
        """
        unifi_data = collections.OrderedDict()
        
        meta = data['meta']
        update_type = meta.get("message", "_unknown_")
        # "events", "device:sync", "device:update", "speed-test:update", "user:sync", "sta:sync", possibly others

        if update_type == "events":
            logger.info('received events message')
            logger.info('\n: %s' % json.dumps(data, indent=2))
            if self.event_q.full():
                # discard oldest event
                self.event_q.get()
                self.event_q.task_done()
            self.event_q.put(data['data'])
        else:
            logger.debug('received %s message' % update_type)
            # logger.debug('\n: %s' % json.dumps(data, indent=2))

        if self.event_q.qsize() > 0:
            logger.info('%d events in event queue%s' %
                        (self.event_q.qsize(), '' if not self.event_q.full() else ' event queue is FULL'))

        if logger.getEffectiveLevel() == logging.DEBUG:    
            with open('raw_data.json', 'w') as f:
                f.write(json.dumps(unifi_data, indent=2))
            
    def events(self, blocking=True):
        """
        returns a list of event updates
        if blocking, waits for a new update, then returns it as a list
        if not blocking, returns any updates in the queue, or an empty list if there are none
        """
        if blocking:
            unifi_events = self.event_q.get()
            self.event_q.task_done()
        else:
            unifi_events = []
            while not self.event_q.empty():
                unifi_events += self.event_q.get()
                self.event_q.task_done()
        return unifi_events


# Initialisation
def init_logging(config):
    log_file = config.get('LOGGING', 'log_file')
    log_level_from_config = config.get('LOGGING', 'log_level', fallback='INFO')
    log_to_console = config.getboolean('LOGGING', 'log_to_console', fallback=False)
    log_level = logging.INFO
    if log_level_from_config == 'DEBUG':
        log_level = logging.DEBUG
    elif log_level_from_config == 'INFO':
        log_level = logging.INFO
    elif log_level_from_config == 'WARNING':
        log_level = logging.WARNING

    # logging to file
    logging.basicConfig(level=log_level, format='%(asctime)s %(levelname)-8s %(message)s',
                        filename=log_file, filemode='w')
    # logging to console
    if log_to_console:
        console = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(levelname)-8s %(message)s')
        console.setFormatter(formatter)
        logging.getLogger('').addHandler(console)

    local_logger = logging.getLogger('main')

    return local_logger


def get_urls_to_call(user_event_url_list, event_list):
    global logger
    url_list = []
    for event in event_list:
        found_user = False
        i = 0
        while not found_user and i < len(user_event_url_list):
            if event['user'] == user_event_url_list[i]['event_user']:
                found_user = True
                found_key = False
                j = 0
                while not found_key and j < len(user_event_url_list[i]['event_action_list']):
                    if event['key'] == user_event_url_list[i]['event_action_list'][j]['event_key']:
                        found_key = True
                        url_list.append(user_event_url_list[i]['event_action_list'][j]['url_to_call'])
                        logger.info('Found %s with event %s' % (user_event_url_list[i]['friendly_name'], event['key']))
                    j += 1
            i += 1

    return url_list


def call_url(url):
    global logger, send_to_server

    if send_to_server:
        logger.debug("About to call '%s'" % url)
        try:
            response = requests.get(url)
            logger.debug("%s -> %s" % (url, response))
            if response.ok:
                logger.info("Successfully called '%s'" % url)
            else:
                logger.warn("Call '%s' failed. Response: %s" % (url, response))
        except requests.ConnectionError as e:
            logger.error('Request failed %s - %s' % (url, e))
    else:
        logger.info("Skipping call '%s'" % url)


def main():
    global logger

    # read configuration
    config = ConfigParser({'send_to_server': "true"})
    config_exists = False
    for loc in os.curdir, os.path.expanduser("~"), os.path.join(os.path.expanduser("~"), "unifi_events_listener"):
        try:
            with open(os.path.join(loc, "unifi_events_listener.conf")) as source:
                config.read_file(source)
                config_exists = True
        except IOError:
            pass

    if not config_exists:
        print("Error: Unable to find the 'unifi_events_listener.conf' file. \n"
              "Put it in the current directory, in ~ or in ~/unifi_events_listener.\n")
        sys.exit(1)

    logger = init_logging(config)

    logger.info("---")
    logger.info("unifi_events_listener, v%s, %s" % (__version__, __updated__))

    # read other configuration
    unifi_creds_file = config.get('UNIFI_CONTROLLER', 'creds_file')
    try:
        with open(unifi_creds_file) as f:
            unifi_user = f.readline().strip()
            unifi_password = f.readline().strip()
    except IOError as e:
        logger.error("Unable to read the apple credentials file '%s': %s" % (unifi_creds_file, str(e)))
        sys.exit(1)

    unifi_ip = config.get('UNIFI_CONTROLLER', 'ip', fallback='localhost')
    unifi_port = config.get('UNIFI_CONTROLLER', 'port', fallback=8443)
    unifi_ssl_verify = config.get('UNIFI_CONTROLLER', 'ssl_verify', fallback=False)

    user_event_url_list = json.loads(config.get('EVENTS', 'user_event_list'))
    send_to_server = config.getboolean('EVENTS', 'send_to_server', fallback=True)

    try:
        client = UnifiClient(unifi_user, unifi_password, unifi_ip, unifi_port, unifi_ssl_verify)

        while True:
            event_list = client.events()
            # logger.info('got new data')
            # logger.info(json.dumps(events, indent=2))
            url_list = get_urls_to_call(user_event_url_list, event_list)
            for url in url_list:
                call_url(url)

    except KeyboardInterrupt:
        logger.info('Program Exit')


if __name__ == '__main__':
    '''
    <Ctrl-C> to exit
    '''
    main()
