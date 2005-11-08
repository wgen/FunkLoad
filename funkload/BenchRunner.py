#!/usr/bin/python
# (C) Copyright 2005 Nuxeo SAS <http://nuxeo.com>
# Author: bdelbosc@nuxeo.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as published
# by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA
# 02111-1307, USA.
#
"""FunkLoad Bench runner.

$Id: BenchRunner.py 24746 2005-08-31 09:59:27Z bdelbosc $
"""

USAGE = """%prog [options] file class.method

%prog launch a FunkLoad unit test as load test.

A FunkLoad unittest use a configuration file named [class].conf, this
configuration is overriden by the command line options.

See http://funkload.nuxeo.org/ for more information.

Examples
========
  %prog myFile.py MyTestCase.testSomething
                        Bench MyTestCase.testSomething using MyTestCase.conf.
  %prog -u http://localhost:8080 -c 10:20 -d 30 myFile.py \\
      MyTestCase.testSomething
                        Bench MyTestCase.testSomething on localhost:8080
                        with 2 cycles of 10 and 20 users during 30s.
  %prog -h
                        More options.
"""
import os
import sys
import time
from datetime import datetime
import traceback
import threading
from socket import error as SocketError
from thread import error as ThreadError
from xmlrpclib import ServerProxy
import unittest
from optparse import OptionParser, TitledHelpFormatter

from utils import mmn_encode, set_recording_flag, recording
from utils import set_running_flag, running
from utils import thread_sleep, trace, red_str, green_str
from utils import get_version




# ------------------------------------------------------------
# utils
#
g_failures = 0                      # result of the bench
g_errors = 0                        # result of the bench
g_success = 0

def add_cycle_result(status):
    """Count number of result."""
    # XXX use a thread.lock, but we don't mind if it is not accurate
    # as the report use the xml log
    global g_success, g_failures, g_errors
    if status == 'success':
        g_success += 1
    elif status == 'error':
        g_errors += 1
    else:
        g_failures += 1

def get_cycle_results():
    """Return counters."""
    global g_success, g_failures, g_errors
    return g_success, g_failures, g_errors

def get_status(success, failures, errors, color=False):
    """Return a status and an exit code."""
    if errors:
        status = 'ERROR'
        if color:
            status = red_str(status)
        code = -1
    elif failures:
        status = 'FAILURE'
        if color:
            status = red_str(status)
        code = 1
    else:
        status = 'SUCCESSFULL'
        if color:
            status = green_str(status)
        code = 0
    return status, code


def reset_cycle_results():
    """Clear the previous results."""
    global g_success, g_failures, g_errors
    g_success = g_failures = g_errors = 0


def load_unittest(test_module, test_class, test_name, options):
    """Instanciate a unittest."""
    module = __import__(test_module)
    klass = getattr(module, test_class)
    return klass(test_name, options)


# ------------------------------------------------------------
# Classes
#
class LoopTestRunner(threading.Thread):
    """Run a unit test in loop."""

    def __init__(self, test_module, test_class, test_name, options,
                 cycle, cvus, thread_id, sleep_time, debug=False):
        meta_method_name = mmn_encode(test_name, cycle, cvus, thread_id)
        threading.Thread.__init__(self, target=self.run, name=meta_method_name,
                                  args=())
        self.test = load_unittest(test_module, test_class, meta_method_name,
                                  options)
        self.color = not options.no_color
        self.sleep_time = sleep_time
        self.debug = debug
        # this makes threads endings if main stop with a KeyboardInterupt
        self.setDaemon(1)


    def run(self):
        """Run a test in loop during."""
        while (running()):
            test_result = unittest.TestResult()
            self.test.clearContext()
            self.test(test_result)
            if test_result.wasSuccessful():
                if recording():
                    add_cycle_result('success')
                    if self.color:
                        trace(green_str('.'))
                    else:
                        trace('.')
            else:
                if len(test_result.errors):
                    if recording():
                        add_cycle_result('error')
                    if self.color:
                        trace(red_str('E'))
                    else:
                        trace('E')
                else:
                    if recording():
                        add_cycle_result('failure')
                    if self.color:
                        trace(red_str('F'))
                    else:
                        trace('F')
                if self.debug:
                    for (test, error) in test_result.errors:
                        trace("ERROR %s: %s" % (str(test), str(error)))
                    for (test, error) in test_result.failures:
                        trace("FAILURE %s: %s" % (str(test), str(error)))
            thread_sleep(self.sleep_time)



# ------------------------------------------------------------
#
#
class BenchRunner:
    """Run a unit test in bench mode."""

    def __init__(self, module_file, class_name, method_name, options):
        self.threads = []
        self.module_name = os.path.basename(os.path.splitext(module_file)[0])
        self.class_name = class_name
        self.method_name = method_name
        self.options = options
        self.color = not options.no_color
        # create a unittest to get the configuration file
        test = load_unittest(self.module_name, class_name,
                             mmn_encode(method_name, 0, 0, 0), options)
        self.config_path = test._config_path
        self.result_path = test.result_path
        self.class_title = test.conf_get('main', 'title')
        self.class_description = test.conf_get('main', 'description')
        self.test_id = self.method_name
        self.test_description = test.conf_get(self.method_name, 'description',
                                              'No test description')
        self.test_url = test.conf_get('main', 'url')
        self.cycles = map(int, test.conf_getList('bench', 'cycles'))
        self.duration = test.conf_getInt('bench', 'duration')
        self.startup_delay = test.conf_getFloat('bench', 'startup_delay')
        self.cycle_time = test.conf_getFloat('bench', 'cycle_time')
        self.sleep_time = test.conf_getFloat('bench', 'sleep_time')
        self.sleep_time_min = test.conf_getFloat('bench', 'sleep_time_min')
        self.sleep_time_max = test.conf_getFloat('bench', 'sleep_time_max')

        # setup monitoring
        monitor_hosts = []                  # list of (host, port, descr)
        for host in test.conf_get('monitor', 'hosts', '', quiet=True).split():
            host = host.strip()
            monitor_hosts.append((host, test.conf_getInt(host, 'port'),
                                  test.conf_get(host, 'description', '')))
        self.monitor_hosts = monitor_hosts
        # keep the test to use the result logger for monitoring
        # and call setUp/tearDown Cycle
        self.test = test


    def run(self):
        """Run all the cycles.

        return 0 on success, 1 if there were some failures and -1 on errors."""
        trace(str(self))
        trace("Benching\n")
        trace("========\n\n")
        cycle = total_success = total_failures = total_errors = 0

        self.logr_open()
        for cvus in self.cycles:
            t_start = time.time()
            reset_cycle_results()
            text = "Cycle #%i with %s virtual users\n" % (cycle, cvus)
            trace(text)
            trace('-' * (len(text) - 1) + "\n\n")
            monitor_key = '%s:%s:%s' % (self.method_name, cycle, cvus)
            self.test.setUpCycle()
            self.startMonitor(monitor_key)
            self.startThreads(cycle, cvus)
            self.logging()
            #self.dumpThreads()
            self.stopThreads()
            self.stopMonitor(monitor_key)
            cycle += 1
            self.test.tearDownCycle()
            t_stop = time.time()
            trace("* End of cycle, %.2fs elapsed.\n" % (t_stop - t_start))
            success, failures, errors = get_cycle_results()
            status, code = get_status(success, failures, errors, self.color)
            trace("* Cycle result: **%s**, "
                  "%i success, %i failure, %i errors.\n\n" % (
                status, success, failures, errors))
            total_success += success
            total_failures += failures
            total_errors += errors
        self.logr_close()

        # display bench result
        trace("Result\n")
        trace("======\n\n")
        trace("* Success: %s\n" % total_success)
        trace("* Failures: %s\n" % total_failures)
        trace("* Errors: %s\n\n" % total_errors)
        status, code = get_status(total_success, total_failures, total_errors)
        trace("Bench status: **%s**\n" % status)
        return code


    def startThreads(self, cycle, number_of_threads):
        """Starting threads."""
        trace("* Current time: %s\n" % datetime.now().isoformat())
        trace("* Starting threads: ")
        threads = []
        i = 0
        set_running_flag(True)
        set_recording_flag(False)
        for thread_id in range(number_of_threads):
            i += 1
            thread = LoopTestRunner(self.module_name, self.class_name,
                                    self.method_name, self.options,
                                    cycle, number_of_threads,
                                    thread_id, self.sleep_time)
            trace(".")
            try:
                thread.start()
            except ThreadError:
                trace("\nERROR: Can not create more than %i threads, try a "
                      "smaller stack size using: 'ulimit -s 2048' "
                      "for example\n" % i)
                raise
            threads.append(thread)
            thread_sleep(self.startup_delay)
        trace(' done.\n')
        self.threads = threads


    def logging(self):
        """Log activity during duration."""
        duration = self.duration
        end_time = time.time() + duration
        trace("* Logging for %ds (untill %s): " % (
            duration, datetime.fromtimestamp(end_time).isoformat()))
        set_recording_flag(True)
        while time.time() < end_time:
            # wait
            time.sleep(1)
        set_recording_flag(False)
        trace(" done.\n")


    def stopThreads(self):
        """Wait for thread endings."""
        set_running_flag(False)
        trace("* Waiting end of threads: ")
        for thread in self.threads:
            thread.join()
            del thread
            trace('.')
        del self.threads
        trace(" done.\n")
        trace("* Waiting cycle sleeptime %ds: ..." % self.cycle_time)
        time.sleep(self.cycle_time)
        trace(" done.\n")


    def dumpThreads(self):
        """Display all different traceback of Threads for debugging.

        Require threadframe module."""
        import threadframe
        stacks = {}
        frames = threadframe.dict()
        for thread_id, frame in frames.iteritems():
            stack = ''.join(traceback.format_stack(frame))
            stacks[stack] = stacks.setdefault(stack, []) + [thread_id]
        def sort_stack(x, y):
            """sort stack by number of thread."""
            return cmp(len(x[1]), len(y[1]))
        stacks = stacks.items()
        stacks.sort(sort_stack)
        for stack, thread_ids in stacks:
            trace('=' * 72 + '\n')
            trace('%i threads : %s\n' % (len(thread_ids), str(thread_ids)))
            trace('-' * 72 + '\n')
            trace(stack + '\n')


    def startMonitor(self, monitor_key):
        """Start monitoring on hosts list."""
        if not self.monitor_hosts:
            return
        monitor_hosts = []
        for (host, port, desc) in self.monitor_hosts:
            trace("* Start monitoring %s: ..." % host)
            server = ServerProxy("http://%s:%s" % (host, port))
            try:
                server.startRecord(monitor_key)
            except SocketError:
                trace(' failed, server is down.\n')
            else:
                trace(' done.\n')
                monitor_hosts.append((host, port, desc))
        self.monitor_hosts = monitor_hosts


    def stopMonitor(self, monitor_key):
        """Stop monitoring and save xml result."""
        if not self.monitor_hosts:
            return
        for (host, port, desc) in self.monitor_hosts:
            trace('* Stop monitoring %s: ' % host)
            server = ServerProxy("http://%s:%s" % (host, port))
            try:
                server.stopRecord(monitor_key)
                xml = server.getXmlResult(monitor_key)
            except SocketError:
                trace(' failed, server is down.\n')
            else:
                trace(' done.\n')
                self.logr(xml)


    def logr(self, message):
        """Log to the test result file."""
        self.test.logr(message, force=True)

    def logr_open(self):
        """Start logging tag."""
        config = {'id': self.test_id,
                  'description': self.test_description,
                  'class_title': self.class_title,
                  'class_description': self.class_description,
                  'module': self.module_name,
                  'class': self.class_name,
                  'method': self.method_name,
                  'cycles': self.cycles,
                  'duration': self.duration,
                  'sleep_time': self.sleep_time,
                  'startup_delay': self.startup_delay,
                  'sleep_time_min': self.sleep_time_min,
                  'sleep_time_max': self.sleep_time_max,
                  'cycle_time': self.cycle_time,
                  'configuration_file': self.config_path,
                  'server_url': self.test_url,
                  'log_xml': self.result_path,}
        for (host, port, desc) in self.monitor_hosts:
            config[host] = desc
        self.test.open_result_log(**config)

    def logr_close(self):
        """Stop logging tag."""
        self.test.close_result_log()

    def __repr__(self):
        """Display bench information."""
        text = []
        text.append('=' * 72)
        text.append('Benching %s.%s' % (self.class_name,
                                        self.method_name))
        text.append('=' * 72)
        text.append(self.test_description)
        text.append('-' * 72 + '\n')
        text.append("Configuration")
        text.append("=============\n")
        text.append("* Current time: %s" % datetime.now().isoformat())
        text.append("* Configuration file: %s" % self.config_path)
        text.append("* Log xml: %s" % self.result_path)
        text.append("* Server: %s" % self.test_url)
        text.append("* Cycles: %s" % self.cycles)
        text.append("* Cycle duration: %ss" % self.duration)
        text.append("* Sleeptime between request: from %ss to %ss" % (
            self.sleep_time_min, self.sleep_time_max))
        text.append("* Sleeptime between test case: %ss" % self.sleep_time)
        text.append("* Startup delay between thread: %ss\n\n" %
                    self.startup_delay)
        return '\n'.join(text)





# ------------------------------------------------------------
# main
#
def main():
    """Default main."""
    # enable to load module in the current path
    cur_path = os.path.abspath(os.path.curdir)
    sys.path.insert(0, cur_path)

    parser = OptionParser(USAGE, formatter=TitledHelpFormatter(),
                          version="FunkLoad %s" % get_version())
    parser.add_option("-u", "--url", type="string", dest="main_url",
                      help="Base URL to bench.")
    parser.add_option("-c", "--cycles", type="string", dest="bench_cycles",
                      help="Cycles to bench, this is a list of number of "
                      "virtual concurrent users, "
                      "to run a bench with 3 cycles with 5, 10 and 20 "
                      "users use: -c 2:10:20")
    parser.add_option("-D", "--duration", type="string", dest="bench_duration",
                      help="Duration of a cycle in seconds.")
    parser.add_option("-m", "--sleep-time-min", type="string",
                      dest="bench_sleep_time_min",
                      help="Minimum sleep time between request.")
    parser.add_option("-M", "--sleep-time-max", type="string",
                      dest="bench_sleep_time_max",
                      help="Maximum sleep time between request.")
    parser.add_option("-s", "--startup-delay", type="string",
                      dest="bench_startup_delay",
                      help="Startup delay between thread.")
    parser.add_option("", "--no-color", action="store_true",
                      help="Monochrome output.")

    options, args = parser.parse_args()
    if len(args) != 2:
        parser.error("incorrect number of arguments")
    if not args[1].count('.'):
        parser.error("invalid argument should be class.method")
    klass, method = args[1].split('.')
    bench = BenchRunner(args[0], klass, method, options)
    ret = bench.run()
    sys.exit(ret)

if __name__ == '__main__':
    main()


