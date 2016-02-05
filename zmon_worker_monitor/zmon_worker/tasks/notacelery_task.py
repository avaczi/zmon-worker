#!/usr/bin/env python
# -*- coding: utf-8 -*-

import ast
from inspect import isclass
import __future__

from collections import Callable, Counter
import socket
from zmon_worker_monitor.zmon_worker.encoder import JsonDataEncoder
from zmon_worker_monitor.zmon_worker.errors import CheckError, InsufficientPermissionsError, SecurityError

import eventlog
import functools
import itertools
import json
import logging
import random
from zmon_worker_monitor.redis_context_manager import RedisConnHandler
import time
import re
import requests
import sys
import setproctitle
from datetime import timedelta, datetime
import urllib
import pytz
import threading
import Queue

from collections import defaultdict

import jsonpath_rw

from bisect import bisect_left
from zmon_worker_monitor.zmon_worker.common.time_ import parse_timedelta

from zmon_worker_monitor.zmon_worker.notifications.hipchat import NotifyHipchat
from zmon_worker_monitor.zmon_worker.notifications.slack import NotifySlack
from zmon_worker_monitor.zmon_worker.notifications.push import NotifyPush

from zmon_worker_monitor.zmon_worker.notifications.mail import Mail
from zmon_worker_monitor.zmon_worker.notifications.sms import Sms
from zmon_worker_monitor.zmon_worker.notifications.hubot import Hubot
from zmon_worker_monitor.zmon_worker.notifications.notification import BaseNotification

from operator import itemgetter
from timeperiod import in_period, InvalidFormat

import functional
from zmon_worker_monitor.zmon_worker.common import mathfun

from zmon_worker_monitor import plugin_manager

import tokens

logger = logging.getLogger(__name__)


# interval in seconds for sending metrics to graphite
METRICS_INTERVAL = 15

DEFAULT_CHECK_RESULTS_HISTORY_LENGTH = 20

TRIAL_RUN_RESULT_EXPIRY_TIME = 300

# we allow specifying condition expressions without using the "value" variable
# the following pattern is used to check if "value" has to be prepended to the condition
SIMPLE_CONDITION_PATTERN = re.compile(r'^[<>!=\[]|i[ns] ')
GRAPHITE_REPLACE_KEYCHARS = re.compile(r'[./\s]')

# round to microseconds
ROUND_SECONDS_DIGITS = 6
JMX_CONFIG_FILE = 'jmxremote.password'
KAIROS_ID_FORBIDDEN_RE = re.compile(r'[^a-zA-Z0-9\-_\.]')

HOST_GROUP_PREFIX = re.compile(r'^([a-z]+)')
INSTANCE_PORT_SUFFIX = re.compile(r':([0-9]+)$')

EVENTS = {
    'ALERT_STARTED': eventlog.Event(0x34001, ['checkId', 'alertId', 'value']),
    'ALERT_ENDED': eventlog.Event(0x34002, ['checkId', 'alertId', 'value']),
    'ALERT_ENTITY_STARTED': eventlog.Event(0x34003, ['checkId', 'alertId', 'value', 'entity']),
    'ALERT_ENTITY_ENDED': eventlog.Event(0x34004, ['checkId', 'alertId', 'value', 'entity']),
    'DOWNTIME_STARTED': eventlog.Event(0x34005, [
        'alertId',
        'entity',
        'startTime',
        'endTime',
        'userName',
        'comment',
    ]),
    'DOWNTIME_ENDED': eventlog.Event(0x34006, [
        'alertId',
        'entity',
        'startTime',
        'endTime',
        'userName',
        'comment',
    ]),
    'SMS_SENT': eventlog.Event(0x34007, ['alertId', 'entity', 'phoneNumber', 'httpStatus']),
    'ACCESS_DENIED': eventlog.Event(0x34008, ['userName', 'entity']),
}

eventlog.register_all(EVENTS)

Sms.register_eventlog_events(EVENTS)

get_value = itemgetter('value')


class ProtectedPartial(object):

    '''
    Provides functools.partial functionality with one additional feature: if keyword arguments contain '__protected'
    key with list of arguments as value, the appropriate values will not be overwritten when calling the partial. This
    way we can prevent user from overwriting internal zmon parameters in check command. The protected key uses double
    underscore to prevent overwriting it, we reject all commands containing double underscores.
    '''

    def __init__(self, func, *args, **kwargs):
        self.__func = func
        self.__partial_args = args
        self.__partial_kwargs = kwargs
        self.__protected = frozenset(kwargs.get('__protected', []))
        self.__partial_kwargs.pop('__protected', None)

    def __call__(self, *args, **kwargs):
        new_kwargs = self.__partial_kwargs.copy()
        new_kwargs.update((k, v) for (k, v) in kwargs.iteritems() if k not in self.__protected)
        return self.__func(*self.__partial_args + args, **new_kwargs)


def propartial(func, *args, **kwargs):
    '''
    >>> propartial(int, base=2)('100')
    4
    >>> propartial(int, base=2)('100', base=16)
    256
    >>> propartial(int, base=2, __protected=['base'])('100', base=16)
    4
    '''

    return ProtectedPartial(func, *args, **kwargs)


normalize_kairos_id = propartial(KAIROS_ID_FORBIDDEN_RE.sub, '_')


def setp(check_id, entity, msg):
    setproctitle.setproctitle('zmon-worker check {} on {} {} {}'.format(check_id, entity, msg,
                              datetime.now().strftime('%H:%M:%S.%f')))


def get_kairosdb_value(name, points, tags):
    return {'name': name, 'datapoints': points, 'tags': tags}


def flatten(structure, key='', path='', flattened=None):
    '''
    >>> flatten({})
    {}
    >>> flatten({'a': {'b': {'c': ['d', 'e']}}})
    {'a.b.c': ['d', 'e']}
    >>> sorted(flatten({'a': {'b': 'c'}, 'd': 'e'}).items())
    [('a.b', 'c'), ('d', 'e')]
    '''
    path = str(path)
    key = str(key)

    if flattened is None:
        flattened = {}
    if not isinstance(structure, dict):
        flattened[((path + '.' if path else '')) + key] = structure
    else:
        for new_key, value in structure.items():
            flatten(value, new_key, '.'.join(filter(None, [path, key])), flattened)
    return flattened


def timed(f):
    '''Decorator to "time" a function execution. Wraps the function's result in a new dict.
    >>> timed(lambda: 1)()['value']
    1
    '''

    def wrapper(*args, **kwargs):
        start = time.time()
        res = f(*args, **kwargs)
        delta = time.time() - start
        # round and use short keys as we will serialize the whole stuff as JSON
        return {'value': res, 'ts': round(start, ROUND_SECONDS_DIGITS), 'td': round(delta, ROUND_SECONDS_DIGITS)}

    return wrapper


def _get_entity_url(entity):
    '''
    >>> _get_entity_url({})

    >>> _get_entity_url({'url': 'fesn01:39820'})
    'http://fesn01:39820'

    >>> _get_entity_url({'host': 'fesn01'})
    'http://fesn01'

    >>> _get_entity_url({'url': 'https://example.org'})
    'https://example.org'
    '''

    if 'url' in entity:
        if entity['url'].startswith('http://') or entity['url'].startswith('https://'):
            return entity['url']

        return 'http://' + entity['url']

    if 'host' in entity:
        return 'http://' + entity['host']

    return None


def _get_jmx_port(entity):
    '''
    >>> _get_jmx_port({'instance': '9620'})
    49620
    '''

    if 'instance' in entity:
        return int('4' + entity['instance'])
    return None


def _get_shards(entity):
    '''
    >>> _get_shards({'shards': {'shard1': 'host1/db1'}})
    {'shard1': 'host1/db1'}

    >>> _get_shards({'service_name': 'db'})
    {'db': 'db/postgres'}

    >>> _get_shards({'service_name': 'db', 'port': 1234})
    {'db': 'db:1234/postgres'}

    >>> _get_shards({'service_name': 'db:1234', 'port': 1234})
    {'db:1234': 'db:1234/postgres'}

    >>> _get_shards({'service_name': 'db:1234'})
    {'db:1234': 'db:1234/postgres'}

    >>> _get_shards({'service_name': 'db-1234', 'port': 1234})
    {'db-1234': 'db-1234:1234/postgres'}

    >>> _get_shards({'project': 'shop'})
    '''

    if 'shards' in entity:
        return entity['shards']
    if 'service_name' in entity:
        return {entity['service_name']: ('{service_name}:{port}/postgres'.format(**entity)
                if 'port' in entity and not entity['service_name'].endswith(':{}'.format(entity['port']))
                else '{}/postgres'.format(entity['service_name']))}
    return None


def entity_values(con, check_id, alert_id,count=1):
    return map(get_value, entity_results(con, check_id, alert_id, count))


def entity_results(con, check_id, alert_id, count=1):
    all_entities = con.hkeys('zmon:alerts:{}:entities'.format(alert_id))
    all_results = []
    for entity_id in all_entities:
        results = get_results(con, check_id, entity_id, count)
        all_results.extend(results)
    return all_results


def capture(value=None, captures=None, **kwargs):
    '''
    >>> capture(1, {})
    1

    >>> captures={}; capture(1, captures); captures
    1
    {'capture_1': 1}

    >>> captures={'capture_1': 1}; capture(2, captures); sorted(captures.items())
    2
    [('capture_1', 1), ('capture_2', 2)]

    >>> captures={}; capture(captures=captures, mykey=1); captures
    1
    {'mykey': 1}

    >>> p = functools.partial(capture, captures={}); p(1); p(a=1)
    1
    1
    '''

    if kwargs:
        if len(kwargs) > 1:
            raise ValueError('Only one named capture supported')
        key, value = kwargs.items()[0]
    else:
        i = 1
        while True:
            key = 'capture_{}'.format(i)
            if key not in captures:
                break
            i += 1
    captures[key] = value
    return value


def _parse_alert_parameter_value(data):
    '''
    >>> _parse_alert_parameter_value({'value': 10})
    10
    >>> _parse_alert_parameter_value({'value': '2014-07-03T22:00:00.000Z', 'comment': "desc", "type": "date"})
    datetime.date(2014, 7, 3)

    '''

    allowed_types = {
        'int': int,
        'float': float,
        'str': str,
        'bool': bool,
        'datetime': lambda json_date: datetime.strptime(json_date, '%Y-%m-%dT%H:%M:%S.%fZ'),
        'date': lambda json_date: datetime.strptime(json_date, '%Y-%m-%dT%H:%M:%S.%fZ').date(),
    }
    value = data['value']
    type_name = data.get('type')
    if type_name:
        try:
            value = allowed_types[type_name](value)
        except Exception:
            raise Exception('Attempted wrong type cast <{}> in alert parameters'.format(type_name))
    return value


def _inject_alert_parameters(alert_parameters, ctx):
    '''
    Inject alert parameters into the execution context dict (ctx)
    '''

    params_name = 'params'
    params = {}

    if alert_parameters:
        for apname, apdata in alert_parameters.items():
            if apname in ctx:
                raise Exception('Parameter name: %s clashes in context', apname)
            value = _parse_alert_parameter_value(apdata)
            params[apname] = value
            ctx[apname] = value  # inject parameter into context

        # inject the whole parameters map so that user can iterate over them in the alert condition
        if params_name not in ctx:
            ctx[params_name] = params


def alert_series(f, n, con, check_id, entity_id):
    """ evaluate given function on the last n check results and return true if the "alert" function f returns true for all values"""

    vs = get_results(con, check_id, entity_id, n)
    active_count = 0
    exception_count = 0

    for v in vs:
        # counting exceptions thrown during eval as alert being active for that interval
        try:
            v = v["value"]
            r = 1 if f(v) else 0
            x =0
        except:
            r = 1
            x = 1

        active_count += r
        exception_count += x

    if exception_count == n:
        raise Exception("All alert evaluations failed!")

    # activating alert if not enough value found (this should only affect starting period)
    return n == active_count or len(vs)<n


def build_condition_context(con, check_id, alert_id, entity, captures, alert_parameters):
    '''
    >>> plugin_manager.collect_plugins(); 'timeseries_median' in build_condition_context(None, 1, 1, {'id': '1'}, {}, {})
    True
    >>> 'timeseries_percentile' in build_condition_context(None, 1, 1, {'id': '1'}, {}, {})
    True
    '''

    history_factory = plugin_manager.get_plugin_obj_by_name('history', 'Function')

    ctx = build_default_context()
    ctx['capture'] = functools.partial(capture, captures=captures)
    ctx['entity_results'] = functools.partial(entity_results, con=con, check_id=check_id, alert_id=alert_id)
    ctx['entity_values'] = functools.partial(entity_values, con=con, check_id=check_id, alert_id=alert_id)
    ctx['entity'] = dict(entity)
    ctx['history'] = history_factory.create({ 'check_id': check_id, 'entity_id_for_kairos': normalize_kairos_id(entity['id']) })
    ctx['value_series'] = functools.partial(get_results_user, con=con, check_id=check_id, entity_id=entity['id'])
    ctx['alert_series'] = functools.partial(alert_series, con=con, check_id=check_id, entity_id=entity['id'])

    _inject_alert_parameters(alert_parameters, ctx)

    for f in (
        mathfun.avg,
        mathfun.delta,
        mathfun.median,
        mathfun.percentile,
        mathfun.first,
        mathfun._min,
        mathfun._max,
        sum,
    ):
        name = f.__name__
        if name.startswith('_'):
            name = name[1:]
        ctx['timeseries_' + name] = functools.partial(_apply_aggregate_function_for_time, con=con, func=f, check_id=check_id,
                                            entity_id=entity['id'], captures=captures)
    return ctx


def _time_slice(time_spec, results):
    '''
    >>> _time_slice('1s', [])
    []

    >>> _time_slice('1s', [{'ts': 0, 'value': 0}, {'ts': 1, 'value': 10}])
    [{'ts': 0, 'value': 0}, {'ts': 1, 'value': 10}]

    >>> _time_slice('2s', [{'ts': 123.6, 'value': 10}, {'ts': 123, 'value': 0}, {'ts': 121, 'value': -10}])
    [{'ts': 123, 'value': 0}, {'ts': 123.6, 'value': 10}]
    '''

    if len(results) < 2:
        # not enough values to calculate anything
        return results
    get_ts = itemgetter('ts')
    results.sort(key=get_ts)
    keys = map(get_ts, results)
    td = parse_timedelta(time_spec)
    last = results[-1]
    needle = last['ts'] - td.total_seconds()
    idx = bisect_left(keys, needle)
    if idx == len(results):
        # timerange exceeds range of results
        return results
    return results[idx:]


def _get_results_for_time(con, check_id, entity_id, time_spec):
    results = get_results(con, check_id, entity_id, DEFAULT_CHECK_RESULTS_HISTORY_LENGTH)
    return _time_slice(time_spec, results)


def _apply_aggregate_function_for_time(
    time_spec,
    con,
    func,
    check_id,
    entity_id,
    captures,
    key=functional.id,
    **args
):

    results = _get_results_for_time(con, check_id, entity_id, time_spec)
    ret = mathfun.apply_aggregate_function(results, func, key=functional.compose(key, get_value), **args)
    # put function result in our capture dict for debugging
    # e.g. captures["delta(5m)"] = 13.5
    captures['{}({})'.format(func.__name__, time_spec)] = ret
    return ret


def _build_notify_context(alert):
    return {
            'True': True,
            'False': False,
            'send_mail': functools.partial(Mail.send, alert),
            'send_email': functools.partial(Mail.send, alert),
            'send_sms': functools.partial(Sms.send, alert),
            'notify_hubot': functools.partial(Hubot.notify, alert),
            'send_hipchat': functools.partial(NotifyHipchat.send, alert),
            'send_slack': functools.partial(NotifySlack.send, alert),
            'send_push': functools.partial(NotifyPush.send, alert)
           }


def _prepare_condition(condition):
    '''function to prepend "value" to condition if necessary

    >>> _prepare_condition('>0')
    'value >0'

    >>> _prepare_condition('["a"]>0')
    'value ["a"]>0'

    >>> _prepare_condition('in (1, 2, 3)')
    'value in (1, 2, 3)'

    >>> _prepare_condition('value>0')
    'value>0'

    >>> _prepare_condition('a in (1, 2, 3)')
    'a in (1, 2, 3)'
    '''

    if SIMPLE_CONDITION_PATTERN.match(condition):
        # short condition format, e.g. ">=3"
        return 'value {}'.format(condition)
    else:
        # condition is more complex, e.g. "value > 3 and value < 10"
        return condition


class PeriodicBufferedAction(object):

    def __init__(self, action, action_name=None, retries=5, t_wait=10, t_random_fraction=0.5):

        self._stop = True
        self.action = action
        self.action_name = action_name if action_name else (action.func_name if hasattr(action, 'func_name') else
                                                            (action.__name__ if hasattr(action, '__name__') else None))
        self.retries = retries
        self.t_wait = t_wait
        self.t_rand_fraction = t_random_fraction

        self._queue = Queue.Queue()
        self._thread = threading.Thread(target=self._loop)
        self._thread.daemon = True

    def start(self):
        self._stop = False
        self._thread.start()

    def stop(self):
        self._stop = True

    def is_active(self):
        return not self._stop

    def get_time_randomized(self):
        return self.t_wait * (1 + random.uniform(-self.t_rand_fraction, self.t_rand_fraction))

    def enqueue(self, data, count=0):
        elem = {
            'data': data,
            'count': count,
            # 'time': time.time()
        }
        try:
            self._queue.put_nowait(elem)
        except Queue.Full:
            logger.exception('Fatal Error: is worker out of memory? Details: ')

    def _collect_from_queue(self):
        elem_list = []
        empty = False

        while not empty and not self._stop:
            try:
                elem_list.append(self._queue.get_nowait())
            except Queue.Empty:
                empty = True
        return elem_list

    def _loop(self):
        t_last = time.time()
        t_wait_last = self.get_time_randomized()

        while not self._stop:
            if time.time() - t_last >= t_wait_last:
                elem_list = self._collect_from_queue()
                try:
                    if elem_list:
                        self.action([e['data'] for e in elem_list])
                except Exception as e:
                    logger.error('Error executing action %s: %s', self.action_name, e)
                    for elem in elem_list:
                        if elem['count'] < self.retries:
                            self.enqueue(elem['data'], count=elem['count']+1)
                        else:
                            logger.error('Error: Maximum retries reached for action %s. Dropping data: %s ',
                                         self.action_name, elem['data'])
                finally:
                    t_last = time.time()
                    t_wait_last = self.get_time_randomized()
            else:
                time.sleep(0.2)  # so loop is responsive to stop commands


def _log_event(event_name, alert, result, entity=None):
    params = {'checkId': alert['check_id'], 'alertId': alert['id'], 'value': result['value']}

    if entity:
        params['entity'] = entity

    eventlog.log(EVENTS[event_name].id, **params)


def _convert_captures(worker_name, alert_id, entity_id, timestamp, captures):
    '''
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c0': 'error'})
    []
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c1': '23.4'})
    [('p0_h.alerts.1.e1.captures.c1', 23.4, 1)]
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c2': 12})
    [('p0_h.alerts.1.e1.captures.c2', 12.0, 1)]
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c3': {'c31': '42'}})
    [('p0_h.alerts.1.e1.captures.c3.c31', 42.0, 1)]
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c4': {'c41': 'error'}})
    []
    >>> _convert_captures('p0.h', 1, 'e .1/2', 1, {'c 1/2': '23.4'})
    [('p0_h.alerts.1.e__1_2.captures.c_1_2', 23.4, 1)]
    >>> _convert_captures('p0.h', 1, 'e1', 1, {'c3': {'c 3.1/': '42'}})
    [('p0_h.alerts.1.e1.captures.c3.c_3_1_', 42.0, 1)]
    '''

    result = []
    key = '{worker_name}.alerts.{alert_id}.{entity_id}.captures.{capture}'

    safe_worker_name = GRAPHITE_REPLACE_KEYCHARS.sub('_', worker_name)
    safe_entity_id = GRAPHITE_REPLACE_KEYCHARS.sub('_', entity_id)

    for capture, value in captures.iteritems():
        safe_capture = GRAPHITE_REPLACE_KEYCHARS.sub('_', capture)
        if isinstance(value, dict):
            for inner_capture, inner_value in value.iteritems():
                try:
                    v = float(inner_value)
                except (ValueError, TypeError):
                    continue
                safe_inner_capture = GRAPHITE_REPLACE_KEYCHARS.sub('_', inner_capture)
                result.append(('{}.{}'.format(key.format(worker_name=safe_worker_name, alert_id=alert_id,
                              entity_id=safe_entity_id, capture=safe_capture), safe_inner_capture), v, timestamp))
        else:
            try:
                v = float(value)
            except (ValueError, TypeError):
                continue
            result.append((key.format(worker_name=safe_worker_name, alert_id=alert_id, entity_id=safe_entity_id,
                          capture=safe_capture), v, timestamp))

    return result



def evaluate_condition(val, condition, **ctx):
    '''

    >>> evaluate_condition(0, '>0')
    False

    >>> evaluate_condition(1, '>0')
    True

    >>> evaluate_condition(1, 'delta("5m")<-10', delta=lambda x:1)
    False

    >>> evaluate_condition({'a': 1}, '["a"]>10')
    False
    '''

    return safe_eval(_prepare_condition(condition), eval_source='<alert-condition>', value=val, **ctx)


class InvalidEvalExpression(Exception):
    pass


class MalformedCheckResult(Exception):

    def __init__(self, msg):
        Exception.__init__(self, msg)


class Try(Callable):

    def __init__(self, try_call, except_call, exc_cls=Exception):
        self.try_call = try_call
        self.except_call = except_call
        self.exc_cls = exc_cls

    def __call__(self, *args):
        try:
            return self.try_call()
        except self.exc_cls, e:
            return self.except_call(e)


def get_results_user(count=1, con=None, check_id=None, entity_id=None):
    return map(lambda x: x["value"], get_results(con, check_id, entity_id, count))


def get_results(con, check_id, entity_id, count=1):
    r = map(json.loads, con.lrange('zmon:checks:{}:{}'.format(check_id, entity_id), 0, count - 1))

    for x in r:
        x.update({"entity_id": entity_id})

    return r


def avg(sequence):
    '''
    >>> avg([])
    0
    >>> avg([1, 2, 3])
    2.0
    >>> avg([2, 3])
    2.5
    '''

    l = len(sequence) * 1.0
    return (sum(sequence) / l if l else 0)


def empty(v):
    '''
    >>> empty([])
    True
    >>> empty([1])
    False
    '''

    return not bool(v)

def jsonpath_flat_filter(obj, path):
    expr = jsonpath_rw.parse(path)
    match = expr.find(obj)
    return dict([(str(m.full_path), m.value) for m in match])

def build_default_context():
    return {
        'abs': abs,
        'all': all,
        'any': any,
        'avg': avg,
        'basestring': basestring,
        'bin': bin,
        'bool': bool,
        'chain': itertools.chain,
        'chr': chr,
        'Counter': Counter,
        'dict': dict,
        'divmod': divmod,
        'Exception': Exception,
        'empty': empty,
        'enumerate': enumerate,
        'False': False,
        'filter': filter,
        'float': float,
        'groupby': itertools.groupby,
        'hex': hex,
        'int': int,
        'isinstance': isinstance,
        'json': json.loads,
        'len': len,
        'list': list,
        'long': long,
        'map': map,
        'max': max,
        'min': min,
        'normalvariate': random.normalvariate,
        'oct': oct,
        'ord': ord,
        'pow': pow,
        'range': range,
        'reduce': functools.reduce,
        'reversed': reversed,
        'round': round,
        'set': set,
        'sorted': sorted,
        'str': str,
        'sum': sum,
        'timestamp': time.time,
        'True': True,
        'Try': Try,
        'tuple': tuple,
        'unichr': unichr,
        'unicode': unicode,
        'xrange': xrange,
        'zip': zip,
        'jsonpath_parse': jsonpath_rw.parse,
        'jsonpath_flat_filter': jsonpath_flat_filter
    }

def check_ast_node_is_safe(node, source):
    '''
    Check that the ast node does not contain any system attribute calls
    as well as exec call (not to construct the system attribute names with strings).

    eval() function calls should not be a problem, as it is hopefuly not exposed
    in the globals and __builtins__

    >>> node = ast.parse('def __call__(): return 1')
    >>> node == check_ast_node_is_safe(node, '<source>')
    True

    >>> check_ast_node_is_safe(ast.parse('def m(): return ().__class__'), '<hidden>')
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <hidden> should not try to access hidden attributes (for example '__class__')


    >>> check_ast_node_is_safe(ast.parse('def horror(g): exec "exploit = ().__" + "class" + "__" in g'), '<horror>')
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <horror> should not try to execute arbitrary code

    '''

    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            if n.attr.startswith('__'):
                raise InvalidEvalExpression("{} should not try to access hidden attributes (for example '__class__')".format(source))
        elif isinstance(n, ast.Exec):
            raise InvalidEvalExpression('{} should not try to execute arbitrary code'.format(source))
    return node


def safe_eval(expr, eval_source='<string>', **kwargs):
    '''
    Safely execute expr.

    For now expr can be only one python expression, a function definition
    or a callable class definition.

    If the expression is returning a callable object (like lambda function
    or Try() object) it will be called and a result of the call will be returned.

    If a result of calling of the defined function or class are returning a callable object
    it will not be called.

    As access to the hidden attributes is protected by check_ast_node_is_safe() method
    we should not have any problem with vulnerabilites defined here:
    Link: http://nedbatchelder.com/blog/201206/eval_really_is_dangerous.html

    TODO: implement compile object cache

    >>> safe_eval('value > 0', value=1)
    True

    >>> safe_eval('def m(): return value', value=10)
    10

    >>> safe_eval('def m(param): return value', value=10)
    Traceback (most recent call last):
        ...
    TypeError: m() takes exactly 1 argument (0 given)

    >>> safe_eval('lambda: value', value=10)
    10

    >>> result = safe_eval('def m(): print value', value=10)
    Traceback (most recent call last):
        ...
    SyntaxError: invalid syntax

    >>> result = safe_eval('print value', value=10)
    Traceback (most recent call last):
        ...
    SyntaxError: invalid syntax

    >>> safe_eval('def m(): return lambda: value', value=10) #doctest: +ELLIPSIS
    <function <lambda> at ...>

    >>> safe_eval('error = value', value=10, eval_source='<alert-condition>')
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <alert-condition> can contain a python expression, a function call or a callable class definition

    >>> safe_eval('def m(): return value.__class__', value=10)
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <string> should not try to access hidden attributes (for example '__class__')

    >>> safe_eval("""
    ... class CallableClass(object):
    ...
    ...     def get_value(self):
    ...         return value
    ...
    ...     def __call__(self):
    ...         return self.get_value()
    ... """, value=10)
    10

    >>> safe_eval("""
    ... class NotCallableClass(object):
    ...
    ...     def get_value(self):
    ...         return value
    ...
    ...     def call(self): # this is not a callable class
    ...         return self.get_value()
    ... """, value=10)
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <string> should contain a callable class definition (missing __call__ method?)


    >>> safe_eval("""
    ... def firstfunc():
    ...     return value
    ...
    ... value > 0
    ...
    ... """, value=10)
    Traceback (most recent call last):
        ...
    InvalidEvalExpression: <string> should contain only one python expression, a function call or a callable class definition

    '''

    g = {'__builtins__': {}, 'object': object, '__name__': __name__}
    # __builtins__ should be masked away to disable builtin functions
    # object is needed if the NewStyle class is being created
    # __name__ is needed to be able to complie a class
    g.update(kwargs)

    node = compile(expr, eval_source, 'exec', ast.PyCF_ONLY_AST | __future__.CO_FUTURE_PRINT_FUNCTION)
    node = check_ast_node_is_safe(node, eval_source)
    body = node.body
    if body and len(body) == 1:
        x = body[0]
        if isinstance(x, ast.FunctionDef) or isinstance(x, ast.ClassDef):
            cc = compile(node, eval_source, 'exec')  # can be nicely cached
            v = {}
            exec (cc, g, v)
            if len(v) == 1:
                c = v.itervalues().next()
                if isclass(c):
                    # we need a class instance and not the class itself
                    c = c()

                if callable(c):
                    return c()  # if a function will return another callable, we will not call it
                else:
                    raise InvalidEvalExpression('{} should contain a callable class definition (missing __call__ method?)'.format(eval_source))
            else:
                raise InvalidEvalExpression('{} should contain only one function or one callable class definition'.format(eval_source))
        elif isinstance(x, ast.Expr):
            cc = compile(expr, eval_source, 'eval', __future__.CO_FUTURE_PRINT_FUNCTION)  # can be nicely cached
            r = eval(cc, g)
            if callable(r):
                # Try() returns callable that should be executed
                return r()
            else:
                return r
        else:
            raise InvalidEvalExpression('{} can contain a python expression, a function call or a callable class definition'.format(eval_source))
    else:
        raise InvalidEvalExpression('{} should contain only one python expression, a function call or a callable class definition'.format(eval_source))


class NotaZmonTask(object):

    abstract = True
    _host = 'localhost'
    _port = 6379
    _secure_queue = 'zmon:queue:secure'
    _db = 0
    _con = None
    _graphite = None
    _counter = Counter()
    _captures_local = []
    _last_metrics_sent = 0
    _last_captures_sent = 0
    _logger = None
    _loglevel = logging.DEBUG
    _kairosdb_enabled = False
    _kairosdb_host = None
    _kairosdb_port = None
    _zmon_url = None
    _worker_name = None
    _queues = None
    _safe_repositories = []

    _is_secure_worker = True

    _timezone = None
    _account = None
    _team = None
    _dataservice_url = None

    _dataservice_poster = None


    _plugin_category = 'Function'
    _plugins = []
    _function_factories = {}
    _zmon_actuator_checkid = None

    @classmethod
    def configure(cls, config):
        try:
            #configure RedisConnHandler
            RedisConnHandler.configure(**config)
        except KeyError:
            logger.exception('Error creating connection: ')
            raise
        #cls._loglevel = (logging.getLevelName(config['loglevel']) if 'loglevel' in config else logging.INFO)
        cls._soap_config = {k: v for k, v in config.items() if k.startswith('soap.service')}
        cls._kairosdb_enabled = config.get('kairosdb.enabled')
        cls._kairosdb_host = config.get('kairosdb.host')
        cls._kairosdb_port = config.get('kairosdb.port')
        cls._zmon_url = config.get('zmon.url')
        cls._queues = config.get('zmon.queues', "zmon:queue:default/16")
        cls._safe_repositories = sorted(config.get('safe_repositories', []))
        cls._zmon_actuator_checkid = config.get('zmon.actuator.checkid', None)

        cls._logger = cls.get_configured_logger()

        cls._is_secure_worker = config.get('worker.is_secure')

        cls._timezone = pytz.timezone('Europe/Berlin')

        cls._account = config.get('account')
        cls._team = config.get('team')

        cls._dataservice_url = config.get('dataservice.url')
        cls._dataservice_oauth2 = config.get('dataservice.oauth2', True)

        if cls._dataservice_url:
            # start action loop for sending reports to dataservice
            cls._logger.info("Enabling data service: {}".format(cls._dataservice_url))
            if cls._dataservice_url and cls._dataservice_oauth2:
                cls._logger.info("Enabling OAUTH2 for data service")
                tokens.configure()
                # TODO: configure proper OAuth scopes
                tokens.manage('uid', ['uid'])
                tokens.start()

            cls._dataservice_poster = PeriodicBufferedAction(cls.send_to_dataservice, retries=10, t_wait=5)
            cls._dataservice_poster.start()

        cls._plugins = plugin_manager.get_plugins_of_category(cls._plugin_category)
        # store function factories from plugins in a dict by name
        cls._function_factories = {p.name: p.plugin_object for p in cls._plugins}

    def __init__(self):
        self.task_context = None
        self._cmds_first_accessed = False

    @classmethod
    def is_secure_worker(cls):
        return cls._is_secure_worker

    @classmethod
    def get_configured_logger(cls):
        if not cls._logger:
            cls._logger = logger
        return cls._logger

    @property
    def con(self):
        self._con = RedisConnHandler.get_instance().get_conn()
        BaseNotification.set_redis_con(self._con)
        return self._con

    @property
    def logger(self):
        return self.get_configured_logger()

    @property
    def worker_name(self):
        if not self._worker_name:
            self._worker_name = 'p{}.{}'.format('local', socket.gethostname())
        return self._worker_name

    def get_redis_host(self):
        return RedisConnHandler.get_instance().get_parsed_redis().hostname

    def get_redis_port(self):
        return RedisConnHandler.get_instance().get_parsed_redis().port

    def send_metrics(self):
        now = time.time()
        if now > self._last_metrics_sent + METRICS_INTERVAL:
            p = self.con.pipeline()
            p.sadd('zmon:metrics', self.worker_name)
            for key, val in self._counter.items():
                p.incrby('zmon:metrics:{}:{}'.format(self.worker_name, key), val)
            p.set('zmon:metrics:{}:ts'.format(self.worker_name), now)
            p.execute()
            self._counter.clear()
            self._last_metrics_sent = now
            # self.logger.info('Send metrics, end storing metrics in redis count: %s, duration: %.3fs', len(self._counter), time.time() - now)

    @classmethod
    def send_to_dataservice(cls, check_results, timeout=10, method='PUT'):

        http_req = {'PUT': requests.put, 'POST': requests.post, 'GET': requests.get}
        headers = {'content-type': 'application/json'}

        if cls._dataservice_oauth2:
            headers.update({'Authorization':'Bearer {}'.format(tokens.get('uid'))})

        team = cls._team if cls._team is not None else ''
        account = cls._account if cls._account is not None else ''

        try:
            # group check_results by check_id
            results_by_id = defaultdict(list)
            for cr in check_results:
                results_by_id[cr['check_id']].append(cr)

            # make separate posts per check_id
            for check_id, results in results_by_id.items():

                url = '{url}/{account}/{check_id}/'.format(url=cls._dataservice_url.rstrip('/'),
                                                           account=urllib.quote(account), check_id=check_id)
                worker_result = {
                    'team': team,
                    'account': account,
                    'results': results,
                }

                # we can skip this data, this problem will never fix itself
                serialized_data = None
                try:
                    serialized_data = json.dumps(worker_result, cls=JsonDataEncoder)
                except Exception as ex:
                    logger.exception("Failed to serialize data for check {} {}: {}".format(check_id, ex, results))

                if serialized_data is not None:
                    r = http_req[method](url, data=serialized_data, timeout=timeout, headers=headers)
                    if r.status_code != requests.codes.ok:
                        raise Exception('http request to {} got status code={}'.format(url, r.status_code))

        except Exception:
            logger.exception("Unexpected error in data service post")
            raise


    def check_and_notify(self, req, alerts, task_context=None):
        self.task_context = task_context
        start_time = time.time()
        # soft_time_limit = req['interval']
        check_id = req['check_id']
        entity_id = req['entity']['id']

        try:
            val = self.check(req)
        #TODO: need to support soft and hard time limits soon
        # except SoftTimeLimitExceeded, e:
        #     self.logger.info('Check request with id %s on entity %s exceeded soft time limit', check_id,
        #                                  entity_id)
        #     # PF-3685 It might happen that this exception was raised after sending a command to redis, but before receiving
        #     # a response. In this case, the connection object is "dirty" and when the same connection gets taken out of the
        #     # pool and reused, it'll throw an exception in redis client.
        #     self.con.connection_pool.disconnect()
        #     notify(check_and_notify, {'ts': start_time, 'td': soft_time_limit, 'value': str(e)}, req, alerts,
        #            force_alert=True)
        except CheckError, e:
            # self.logger.warn('Check failed for request with id %s on entity %s. Output: %s', check_id, entity_id, str(e))
            self.notify({'ts': start_time, 'td': time.time() - start_time, 'value': str(e), 'worker': self.worker_name, 'exc': 1}, req, alerts,
                   force_alert=True)
        except SecurityError, e:
            self.logger.exception('Security exception in request with id %s on entity %s', check_id, entity_id)
            self.notify({'ts': start_time, 'td': time.time() - start_time, 'value': str(e), 'worker': self.worker_name, 'exc': 1}, req, alerts,
                   force_alert=True)
        except Exception, e:
            # self.logger.exception('Check request with id %s on entity %s threw an exception', check_id, entity_id)
            # PF-3685 Disconnect on unknown exceptions: we don't know what actually happened, it might be that redis
            # connection is dirty. CheckError exception is "safe", it's thrown by the worker whenever the check returns a
            # different response than expected, the user doesn't have access to the checked entity or there's an error in
            # check's parameters.
            self.con.connection_pool.disconnect()
            self.notify({'ts': start_time, 'td': time.time() - start_time, 'value': str(e), 'worker': self.worker_name, 'exc': 1}, req, alerts,
                   force_alert=True)
        else:
            self.notify(val, req, alerts)

    def trial_run(self, req, alerts, task_context=None):
        self.task_context = task_context
        start_time = time.time()
        # soft_time_limit = req['interval']
        entity_id = req['entity']['id']

        try:
            val = self.check_for_trial_run(req)
        #TODO: need to support soft and hard time limits soon
        # except SoftTimeLimitExceeded, e:
        #     trial_run.logger.info('Trial run on entity %s exceeded soft time limit', entity_id)
        #     trial_run.con.connection_pool.disconnect()
        #     notify_for_trial_run(trial_run, {'ts': start_time, 'td': soft_time_limit, 'value': str(e)}, req, alerts,
        #                          force_alert=True)
        except InsufficientPermissionsError, e:
            self.logger.info('Access denied for user %s to run check on %s', req['created_by'], entity_id)
            eventlog.log(EVENTS['ACCESS_DENIED'].id, userName=req['created_by'], entity=entity_id)
            self.notify_for_trial_run({'ts': start_time, 'td': time.time() - start_time, 'value': str(e)}, req,
                                 alerts, force_alert=True)
        except CheckError, e:
            self.logger.warn('Trial run on entity %s failed. Output: %s', entity_id, str(e))
            self.notify_for_trial_run({'ts': start_time, 'td': time.time() - start_time, 'value': str(e)}, req,
                                 alerts, force_alert=True)
        except Exception, e:
            self.logger.exception('Trial run on entity %s threw an exception', entity_id)
            self.con.connection_pool.disconnect()
            self.notify_for_trial_run({'ts': start_time, 'td': time.time() - start_time, 'value': str(e)}, req,
                                 alerts, force_alert=True)
        else:
            self.notify_for_trial_run(val, req, alerts)


    def cleanup(self, *args, **kwargs):
        self.task_context = kwargs.get('task_context')
        p = self.con.pipeline()
        p.smembers('zmon:checks')
        p.smembers('zmon:alerts')
        check_ids, alert_ids = p.execute()

        for check_id in kwargs.get('disabled_checks', {}):
            self._cleanup_check(p, check_id)

        for alert_id in kwargs.get('disabled_alerts', {}):
            self._cleanup_alert(p, alert_id)

        for check_id in check_ids:
            if check_id in kwargs.get('check_entities', {}):
                redis_entities = self.con.smembers('zmon:checks:{}'.format(check_id))
                check_entities = set(kwargs['check_entities'][check_id])

                # If it happens that we remove all entities for given check, we should remove all the things.
                if not check_entities:
                    p.srem('zmon:checks', check_id)
                    p.delete('zmon:checks:{}'.format(check_id))
                    for entity in redis_entities:
                        p.delete('zmon:checks:{}:{}'.format(check_id, entity))
                else:
                    self._cleanup_common(p, 'checks', check_id, redis_entities - check_entities)
            else:

                self._cleanup_check(p, check_id)

        for alert_id in alert_ids:
            if alert_id in kwargs.get('alert_entities', {}):
                # Entities that are in the alert state.
                redis_entities = self.con.smembers('zmon:alerts:{}'.format(alert_id))
                alert_entities = set(kwargs['alert_entities'][alert_id])

                # If it happens that we remove all entities for given alert, we should remove all the things.
                if not alert_entities:
                    p.srem('zmon:alerts', alert_id)
                    p.delete('zmon:alerts:{}'.format(alert_id))
                    p.delete('zmon:alerts:{}:entities'.format(alert_id))
                    for entity in redis_entities:
                        p.delete('zmon:alerts:{}:{}'.format(alert_id, entity))
                        p.delete('zmon:notifications:{}:{}'.format(alert_id, entity))
                else:
                    self._cleanup_common(p, 'alerts', alert_id, redis_entities - alert_entities)
                    # All entities matching given alert definition.
                    all_entities = set(self.con.hkeys('zmon:alerts:{}:entities'.format(alert_id)))
                    for entity in all_entities - alert_entities:
                        self.logger.info('Removing entity %s from hash %s', entity,
                                            'zmon:alerts:{}:entities'.format(alert_id))
                        p.hdel('zmon:alerts:{}:entities'.format(alert_id), entity)
                        p.delete('zmon:notifications:{}:{}'.format(alert_id, entity))
            else:
                self._cleanup_alert(p, alert_id)

        p.execute()


    def _cleanup_check(self, pipeline, check_id):
        self.logger.info('Removing check with id %s from zmon:checks set', check_id)
        pipeline.srem('zmon:checks', check_id)
        for entity_id in self.con.smembers('zmon:checks:{}'.format(check_id)):
            self.logger.info('Removing key %s', 'zmon:checks:{}:{}'.format(check_id, entity_id))
            pipeline.delete('zmon:checks:{}:{}'.format(check_id, entity_id))
        self.logger.info('Removing key %s', 'zmon:checks:{}'.format(check_id))
        pipeline.delete('zmon:checks:{}'.format(check_id))


    def _cleanup_alert(self, pipeline, alert_id):
        self.logger.info('Removing alert with id %s from zmon:alerts set', alert_id)
        pipeline.srem('zmon:alerts', alert_id)
        for entity_id in self.con.smembers('zmon:alerts:{}'.format(alert_id)):
            self.logger.info('Removing key %s', 'zmon:alerts:{}:{}'.format(alert_id, entity_id))
            pipeline.delete('zmon:alerts:{}:{}'.format(alert_id, entity_id))
            pipeline.delete('zmon:notifications:{}:{}'.format(alert_id, entity_id))
        self.logger.info('Removing key %s', 'zmon:alerts:{}'.format(alert_id))
        pipeline.delete('zmon:alerts:{}'.format(alert_id))
        self.logger.info('Removing key %s', 'zmon:alert:{}:entities'.format(alert_id))
        pipeline.delete('zmon:alerts:{}:entities'.format(alert_id))


    def _cleanup_common(self, pipeline, entry_type, entry_id, entities):
        '''
        Removes entities from redis matching given type and id.
        Parameters
        ----------
        entry_type: str
            Type of entry to remove: 'checks' or 'alerts'.
        entry_id: int
            Id of entry to remove.
        entities: set
            A set of entities to remove (difference between entities from scheduler and ones present in redis).
        '''

        for entity in entities:
            self.logger.info('Removing entity %s from set %s', entity, 'zmon:{}:{}'.format(entry_type, entry_id))
            pipeline.srem('zmon:{}:{}'.format(entry_type, entry_id), entity)
            self.logger.info('Removing key %s', 'zmon:{}:{}:{}'.format(entry_type, entry_id, entity))
            pipeline.delete('zmon:{}:{}:{}'.format(entry_type, entry_id, entity))

    def _store_check_result(self, req, result):
        self.con.sadd('zmon:checks', req['check_id'])
        self.con.sadd('zmon:checks:{}'.format(req['check_id']), req['entity']['id'])
        key = 'zmon:checks:{}:{}'.format(req['check_id'], req['entity']['id'])
        value = json.dumps(result, cls=JsonDataEncoder)
        self.con.lpush(key, value)
        self.con.ltrim(key, 0, DEFAULT_CHECK_RESULTS_HISTORY_LENGTH - 1)


    def check(self, req):

        self.logger.debug(req)
        # schedule_time = req['schedule_time']
        start = time.time()

        try:
            setp(req['check_id'], req['entity']['id'], 'start')
            res = self._get_check_result(req)
            setp(req['check_id'], req['entity']['id'], 'done')
        except Exception, e:
            # PF-3778 Always store check results and re-raise exception which will be handled in 'check_and_notify'.
            self._store_check_result(req, {'td': round(time.time() - start, ROUND_SECONDS_DIGITS), 'ts': round(start,
                                ROUND_SECONDS_DIGITS), 'value': str(e), 'worker': self.worker_name, 'exc': 1})
            raise
        finally:
            # Store duration in milliseconds as redis only supports integers for counters.

            # 'check.{}.count'.format(req['check_id']): 1,
            # 'check.{}.duration'.format(req['check_id']): int(round(1000.0 * (time.time() - start))),
            # 'check.{}.latency'.format(req['check_id']): int(round(1000.0 * (start - schedule_time))),

            self._counter.update({
                'check.count': 1
            })

            self.send_metrics()

        setp(req['check_id'], req['entity']['id'], 'store')
        self._store_check_result(req, res)
        setp(req['check_id'], req['entity']['id'], 'store kairos')

        try:
            self._store_check_result_to_kairosdb(req, res)
        except:
            pass

        setp(req['check_id'], req['entity']['id'], 'stored')

        return res


    def check_for_trial_run(self, req):
        # fake check ID as it is used by check context
        req['check_id'] = 'trial_run'
        return self._get_check_result(req)


    @timed
    def _get_check_result_internal(self, req):

        self._enforce_security(req)
        cmd = req['command']

        ctx = self._build_check_context(req)
        try:
            result = safe_eval(cmd, eval_source='<check-command>', **ctx)
            return result() if isinstance(result, Callable) else result

        except (SyntaxError, InvalidEvalExpression), e:
            raise CheckError(str(e))

    def _get_check_result(self, req):
        r = self._get_check_result_internal(req)
        r['worker'] = self.worker_name
        return r

    def _enforce_security(self, req):
        '''
        Check tasks from the secure queue to asert the command to run is specified in scm check definition
        Side effect: modifies req to address unique security concerns
        Raises SecurityError on check failure
        '''

        if self.is_secure_worker() or self.task_context['delivery_info'].get('routing_key') == 'secure':
            try:
                # TODO: either implement SCM command loading or remove all related code
                scm_commands = []  # self.load_scm_commands(self._safe_repositories)
            except Exception, e:
                traceback = sys.exc_info()[2]
                raise SecurityError('Unexpected Internal error: {}'.format(e)), None, traceback

            if req['command'] not in scm_commands:
                raise SecurityError('Security violation: Non-authorized command received in secure environment')

            # transformations of entities: hostname "pp-whatever" needs to become "whatever.pp"
            prefix = 'pp-'
            if 'host' in req['entity'] and str(req['entity']['host']).startswith(prefix):

                self.logger.warn('secure req[entity] before pp- transformations: %s', req['entity'])

                real_host = req['entity']['host']
                #secure_host = '{}.pp'.format(req['entity']['host'][3:])
                secure_host = '{}.{}'.format(req['entity']['host'][len(prefix):], prefix[:-1])
                # relplace all real host values occurrences with secure_host
                req['entity'].update({k: v.replace(real_host, secure_host) for k, v in req['entity'].items() if
                                      isinstance(v, basestring) and real_host in v and k != 'id'})

                self.logger.warn('secure req[entity] after pp- transformations: %s', req['entity'])



    def _build_check_context(self, req):
        '''Build context for check command with all necessary functions'''

        entity = req['entity']

        # function creation context: passed to function factories create() method
        factory_ctx = {
            'entity': entity,
            'entity_url': _get_entity_url(entity),
            'check_id': req['check_id'],
            'entity_id': entity['id'],
            'host': entity.get('host'),
            'port': entity.get('port'),
            'instance': entity.get('instance'),
            'external_ip': entity.get('external_ip'),
            'load_balancer_status': entity.get('load_balancer_status'),
            'data_center_code': entity.get('data_center_code'),
            'database': entity.get('database'),
            'jmx_port': _get_jmx_port(entity),
            'shards': _get_shards(entity),
            'soft_time_limit': req['interval'],
            'redis_host': self.get_redis_host(),
            'redis_port': self.get_redis_port(),
            'zmon_url': NotaZmonTask._zmon_url,
            'entity_id_for_kairos': normalize_kairos_id(entity['id']),
            'req_created_by': req.get('created_by'),
        }

        # check execution context
        ctx = build_default_context()
        ctx['entity'] = entity

        # populate check context with functions from plugins' function factories
        for func_name, func_factory in self._function_factories.items():
            if func_name not in ctx:
                ctx[func_name] = func_factory.create(factory_ctx)
        return ctx

    def _store_check_result_to_kairosdb(self, req, result):

        if not self._kairosdb_enabled:
            return

        def get_host_data(entity):
            d = {"entity": normalize_kairos_id(entity["id"])}

            # FIXME; hardcoded list of entity types :-(
            if entity["type"] not in ["host", "zomcat", "zompy"]:
                return d

            # FIXME: hardcoded DC prefix
            id = entity["id"].replace('itr-','').replace('gth-', '')

            m = HOST_GROUP_PREFIX.search(id)
            if m:
                d["hg"] = m.group(0)

            if 'ports' not in entity:
                m = INSTANCE_PORT_SUFFIX.search(id)
                if m:
                    d["port"] = m.group(1)
            else:
                d["port"] = str(entity['ports'].items()[-1:][0][1])

            return d

        # use tags in kairosdb to reflect top level keys in result
        # zmon.check.<checkid> as key for time series

        series_name = 'zmon.check.{}'.format(req['check_id'])

        values = []

        host_tags = get_host_data(req["entity"])

        if isinstance(result['value'], dict):

            if '_use_scheduled_time' in result['value']:
                ts = int(req['schedule_time'] * 1000)
                del result['value']['_use_scheduled_time']
            else:
                ts = int(result['ts'] * 1000)

            flat_result = flatten(result['value'])

            for k, v in flat_result.iteritems():

                try:
                    v = float(v)
                except (ValueError, TypeError):
                    continue

                points = [[ts, v]]
                tags = {'key': normalize_kairos_id(str(k))}

                key_split = tags['key'].split('.')
                metric_tag = key_split[-1]
                if not metric_tag:
                    #should only happen for key ending with a "." and as it is a dict there then exists a -2
                    metric_tag = key_split[-2]
                tags['metric'] = metric_tag

                # FIXME: hardcoded check ID :-(
                if req['check_id'] == 2115:
                    status_code = key_split[-2]
                    tags['sc'] = status_code
                    tags['sg'] = status_code[:1]

                tags.update(host_tags)

                values.append(get_kairosdb_value(series_name, points, tags))
        else:
            try:
                v = float(result['value'])
            except (ValueError, TypeError):
                pass
            else:
                points = [[int(result['ts'] * 1000), v]]

                tags = {}
                tags.update(host_tags)

                values.append(get_kairosdb_value(series_name, points, tags))

        if len(values) > 0:
            self.logger.debug(values)
            try:
                r = requests.post('http://{}:{}/api/v1/datapoints'.format(self._kairosdb_host, self._kairosdb_port),
                                  json.dumps(values), timeout=2)
                if r.status_code not in [200, 204]:
                    self.logger.error(r.text)
                    self.logger.error(json.dumps(values))
            except Exception, e:
                self.logger.error("KairosDB write failed {}".format(e))


    def evaluate_alert(self, alert_def, req, result):
        '''Check if the result triggers an alert

        The function will save the global alert state to the following redis keys:

        * zmon:alerts:<ALERT-DEF-ID>:entities    hash of entity IDs -> captures
        * zmon:alerts                            set of active alert definition IDs
        * zmon:alerts:<ALERT-DEF-ID>             set of entity IDs in alert
        * zmon:alerts:<ALERT-DEF-ID>:<ENTITY-ID> JSON with alert evaluation result for given alert definition and entity
        '''

        # captures is our map of "debug" information, e.g. to see values calculated in our condition
        captures = {}

        alert_id = alert_def['id']
        check_id = alert_def['check_id']
        alert_parameters = alert_def.get('parameters')

        try:
            result = evaluate_condition(result['value'], alert_def['condition'], **build_condition_context(self.con,
                                        check_id, alert_id, req['entity'], captures, alert_parameters))
        except Exception, e:
            captures['exception'] = str(e)
            result = True

        try:
            is_alert = bool((result() if isinstance(result, Callable) else result))
        except Exception, e:
            captures['exception'] = str(e)
            is_alert = True

        # add parameters to captures so they can be substituted in alert title
        if alert_parameters:
            pure_captures = captures.copy()
            try:
                captures = {k: p['value'] for k, p in alert_parameters.items()}
            except Exception, e:
                self.logger.exception('Error when capturing parameters: ')
            captures.update(pure_captures)

        return is_alert, captures


    def send_notification(self, notification, context):
        ctx = _build_notify_context(context)
        try:
            repeat = safe_eval(notification, eval_source='<check-command>' , **ctx)
        except Exception, e:
            # TODO Define what should happen if sending emails or sms fails.
            self.logger.exception(e)
        else:
            if repeat:
                self.con.hset('zmon:notifications:{}:{}'.format(context['alert_def']['id'], context['entity']['id']),
                              notification, time.time() + repeat)

    def notify(self, val, req, alerts, force_alert=False):
        '''
        Process check result and evaluate all alerts. Returns list of active alert IDs
        Parameters
        ----------
        val: dict
            Check result, see check function
        req: dict
            Check request dict
        alerts: list
            A list of alert definitions matching the checked entity
        force_alert: bool
            An optional flag whether to skip alert evalution and force "in alert" state. Used when check request exceeds
            time limit or throws other exception, this way unexpected conditions are always treated as alerts.
        Returns
        -------
        list
            A list of alert definitions matching given entity.
        '''

        def ts_serialize(ts):
            return datetime.fromtimestamp(ts, tz=self._timezone).isoformat(' ') if ts else None

        result = []
        entity_id = req['entity']['id']
        start = time.time()

        check_result = {
            'time': ts_serialize(val.get('ts')) if isinstance(val, dict) else None,
            'run_time': val.get('td') if isinstance(val, dict) else None,  # TODO: should be float or is it milliseconds?
            'check_id': req['check_id'],
            'entity_id': req['entity']['id'],
            'check_result': val,
            'exception': True if isinstance(val, dict) and val.get('exc') else False,
            'alerts': {},
        }

        try:
            setp(req['check_id'], entity_id, 'notify loop')
            for alert in alerts:
                alert_id = alert['id']
                alert_entities_key = 'zmon:alerts:{}'.format(alert_id)
                alerts_key = 'zmon:alerts:{}:{}'.format(alert_id, entity_id)
                notifications_key = 'zmon:notifications:{}:{}'.format(alert_id, entity_id)
                is_alert, captures = ((True, {}) if force_alert else self.evaluate_alert(alert, req, val))

                func = getattr(self.con, ('sadd' if is_alert else 'srem'))
                changed = bool(func(alert_entities_key, entity_id))

                if is_alert:
                    # bubble up: also update global set of alerts
                    alert_changed = func('zmon:alerts', alert_id)

                    if alert_changed:
                        _log_event('ALERT_STARTED', alert, val)
                else:
                    entities_in_alert = self.con.smembers(alert_entities_key)
                    if not entities_in_alert:
                        # no entity has alert => remove from global set
                        alert_changed = func('zmon:alerts', alert_id)
                        if alert_changed:
                            _log_event('ALERT_ENDED', alert, val)

                # PF-3318 If an alert has malformed time period, we should evaluate it anyway and continue with
                # the remaining alert definitions.
                try:
                    is_in_period = in_period(alert.get('period', ''))
                except InvalidFormat, e:
                    self.logger.warn('Alert with id %s has malformed time period.', alert_id)
                    captures['exception'] = '; \n'.join(filter(None, [captures.get('exception'), str(e)]))
                    is_in_period = True

                if changed and is_in_period and is_alert:
                    # notify on entity-level
                    _log_event(('ALERT_ENTITY_STARTED'), alert, val, entity_id)
                elif changed and not is_alert:
                    _log_event(('ALERT_ENTITY_ENDED'), alert, val, entity_id)


                # Always store captures for given alert-entity pair, this is also used a list of all entities matching
                # given alert id. Captures are stored here because this way we can easily link them with check results
                # (see PF-3146).
                self.con.hset('zmon:alerts:{}:entities'.format(alert_id), entity_id, json.dumps(captures,
                              cls=JsonDataEncoder))

                # prepare report - alert part
                check_result['alerts'][alert_id] = {
                    'alert_id': alert_id,
                    'captures': captures,
                    'downtimes': [],
                    'exception': True if isinstance(captures, dict) and 'exception' in captures else False,
                    'active':  is_alert,
                    'changed': changed,
                    'in_period': is_in_period,
                    'start_time': None,
                    # '_alert_stored': None,
                }

                # get last alert data stored in redis if any
                alert_stored = None
                try:
                    stored_raw = self.con.get(alerts_key)
                    alert_stored = json.loads(stored_raw) if stored_raw else None
                except (ValueError, TypeError):
                    self.logger.warn('My messy Error parsing JSON alert result for key: %s', alerts_key)

                if False:
                    # get notification data stored in redis if any
                    try:
                        stored_raw = self.con.get(notifications_key)
                        json.loads(stored_raw) if stored_raw else None
                    except (ValueError, TypeError):
                        self.logger.warn('My requete-messy Error parsing JSON alert result for key: %s', notifications_key)

                downtimes = None

                if is_in_period:

                    self._counter.update({'alerts.{}.count'.format(alert_id): 1,
                                         'alerts.{}.evaluation_duration'.format(alert_id):
                                         int(round(1000.0 * (time.time() - start)))})

                    # Always evaluate downtimes, so that we don't miss downtime_ended event in case the downtime ends when
                    # the alert is no longer active.
                    downtimes = self._evaluate_downtimes(alert_id, entity_id)

                    start_time = time.time()

                    # Store or remove the check value that triggered the alert
                    if is_alert:
                        result.append(alert_id)
                        start_time = alert_stored['start_time'] if alert_stored and not changed else time.time()

                        # create or refresh stored alert
                        alert_stored = dict(captures=captures, downtimes=downtimes, start_time=start_time, **val)
                        self.con.set(alerts_key, json.dumps(alert_stored, cls=JsonDataEncoder))
                    else:
                        self.con.delete(alerts_key)
                        self.con.delete(notifications_key)

                    start = time.time()
                    notification_context = {
                        'alert_def': alert,
                        'entity': req['entity'],
                        'value': val,
                        'captures': captures,
                        'worker': self.worker_name,
                        'is_alert': is_alert,
                        'changed': changed,
                        'duration': timedelta(seconds=(time.time() - start_time if is_alert and not changed else 0)),
                    }

                    #do not send notifications for downtimed alerts
                    if not downtimes:
                        if changed:
                            if 'notifications' not in alert:
                                alert['notifications'] = ['send_push()']

                            # do not overwrite custom push notification
                            if len(filter(lambda x: x.startswith('send_push'), alert['notifications'])) <= 0:
                                alert['notifications'].append('send_push()')

                            for notification in alert['notifications']:
                                self.send_notification(notification, notification_context)
                        else:
                            previous_times = self.con.hgetall(notifications_key)
                            for notification in alert['notifications']:
                                if notification in previous_times and time.time() > float(previous_times[notification]):
                                    self.send_notification(notification, notification_context)

                    self._counter.update({'alerts.{}.notification_duration'.format(alert_id):
                                         int(round(1000.0 * (time.time() - start)))})
                    setp(req['check_id'], entity_id, 'notify loop - send metrics')
                    self.send_metrics()
                    setp(req['check_id'], entity_id, 'notify loop end')
                else:
                    self.logger.debug('Alert %s is not in time period: %s', alert_id, alert['period'])
                    if is_alert:
                        entities_in_alert = self.con.smembers('zmon:alerts:{}'.format(alert_id))

                        p = self.con.pipeline()
                        p.srem('zmon:alerts:{}'.format(alert_id), entity_id)
                        p.delete('zmon:alerts:{}:{}'.format(alert_id, entity_id))
                        p.delete(notifications_key)
                        if len(entities_in_alert) == 1:
                            p.srem('zmon:alerts', alert_id)
                        p.execute()

                        self.logger.info('Removed alert with id %s on entity %s from active alerts due to time period: %s',
                                         alert_id, entity_id, alert.get('period', ''))

                # add to alert report regardless alert up/down/out of period
                # report['results']['alerts'][alert_id]['_alert_stored'] = alert_stored
                # report['results']['alerts'][alert_id]['_notifications_stored'] = notifications_stored

                check_result['alerts'][alert_id]['start_time'] = ts_serialize(alert_stored['start_time']) if alert_stored else None
                check_result['alerts'][alert_id]['start_time_ts'] = alert_stored['start_time'] if alert_stored else None
                check_result['alerts'][alert_id]['downtimes'] = downtimes

            setp(req['check_id'], entity_id, 'return notified')

            # enqueue report to be sent via http request
            if self._dataservice_poster:
                #'entity_id': req['entity']['id'],
                check_result["entity"] = {"id": req['entity']['id']}

                for k in ["application_id","application_version","stack_name","stack_version","team","account_alias"]:
                    if k in req["entity"]:
                        check_result["entity"][k] = req["entity"][k]

                self._dataservice_poster.enqueue(check_result)

            return result
        #TODO: except SoftTimeLimitExceeded:
        except Exception:
            # Notifications should not exceed the time limit.
            self.logger.exception('Notification for check %s reached soft time limit', req['check_name'])
            self.con.connection_pool.disconnect()
            return None


    def post_trial_run(self, id, entity, result):
        if self._dataservice_url is not None:

            val = {
                'id': id,
                'entity-id': entity,
                'result': result
            }

            headers = {"Content-Type":"application/json"}
            if self._dataservice_oauth2:
                headers.update({'Authorization':'Bearer {}'.format(tokens.get('uid'))})

            try:
                requests.put(self._dataservice_url+"trial-run/", data=json.dumps(val, cls=JsonDataEncoder), headers=headers)
            except Exception as ex:
                self.logger.exception(ex)


    def notify_for_trial_run(self, val, req, alerts, force_alert=False):
        """Like notify(), but for trial runs!"""

        try:
            # There must be exactly one alert in alerts.
            alert,  = alerts
            redis_key = 'zmon:trial_run:{uuid}:results'.format(uuid=(alert['id'])[3:])

            is_alert, captures = ((True, {}) if force_alert else self.evaluate_alert(alert, req, val))

            try:
                is_in_period = in_period(alert.get('period', ''))
            except InvalidFormat, e:
                self.logger.warn('Alert with id %s has malformed time period.', alert['id'])
                captures['exception'] = '; \n'.join(filter(None, [captures.get('exception'), str(e)]))
                is_in_period = True

            try:
                result = {
                    'entity': req['entity'],
                    'value': val,
                    'captures': captures,
                    'is_alert': is_alert,
                    'in_period': is_in_period,
                }
                result_json = json.dumps(result, cls=JsonDataEncoder)
            except TypeError, e:
                result = {
                    'entity': req['entity'],
                    'value': str(e),
                    'captures': {},
                    'is_alert': is_alert,
                    'in_period': is_in_period,
                }
                result_json = json.dumps(result, cls=JsonDataEncoder)

            self.con.hset(redis_key, req['entity']['id'], result_json)
            self.con.expire(redis_key, TRIAL_RUN_RESULT_EXPIRY_TIME)

            self.post_trial_run(alert['id'][3:], req['entity'], result)

            return ([alert['id']] if is_alert and is_in_period else [])

        #TODO: except SoftTimeLimitExceeded:
        except Exception:
            self.con.connection_pool.disconnect()
            return None


    def _store_captures_locally(self, alert_id, entity_id, timestamp, captures):
        metrics = _convert_captures(self.worker_name, alert_id, entity_id, timestamp, captures)
        if metrics:
            self._captures_local.extend(metrics)



    def _evaluate_downtimes(self, alert_id, entity_id):
        result = []

        p = self.con.pipeline()
        p.smembers('zmon:downtimes:{}'.format(alert_id))
        p.hgetall('zmon:downtimes:{}:{}'.format(alert_id, entity_id))
        redis_entities, redis_downtimes = p.execute()

        try:
            downtimes = dict((k, json.loads(v)) for (k, v) in redis_downtimes.iteritems())
        except ValueError, e:
            self.logger.exception(e)
        else:
            now = time.time()
            for uuid, d in downtimes.iteritems():
                # PF-3604 First check if downtime is active, otherwise check if it's expired, else: it's a future downtime.
                if now > d['start_time'] and now < d['end_time']:
                    d['id'] = uuid
                    result.append(d)
                    func = 'sadd'
                elif now >= d['end_time']:
                    func = 'srem'
                else:
                    continue

                # Check whether the downtime changed state: active -> inactive or inactive -> active.
                changed = getattr(self.con, func)('zmon:active_downtimes', '{}:{}:{}'.format(alert_id, entity_id, uuid))
                if changed:
                    eventlog.log(EVENTS[('DOWNTIME_ENDED' if func == 'srem' else 'DOWNTIME_STARTED')].id, **{
                        'alertId': alert_id,
                        'entity': entity_id,
                        'startTime': d['start_time'],
                        'endTime': d['end_time'],
                        'userName': d['created_by'],
                        'comment': d['comment'],
                    })

                # If downtime is over, we can remove its definition from redis.
                if func == 'srem':
                    if len(downtimes) == 1:
                        p.delete('zmon:downtimes:{}:{}'.format(alert_id, entity_id))
                        if len(redis_entities) == 1:
                            p.delete('zmon:downtimes:{}'.format(alert_id))
                            p.srem('zmon:downtimes', alert_id)
                        else:
                            p.srem('zmon:downtimes:{}'.format(alert_id), entity_id)
                    else:
                        p.hdel('zmon:downtimes:{}:{}'.format(alert_id, entity_id), uuid)
                    p.execute()

        return result
