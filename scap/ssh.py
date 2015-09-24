# -*- coding: utf-8 -*-
"""
    scap.ssh
    ~~~~~~~~
    This module provides functions for running commands on remote hosts
    via SSH.

"""
import errno
import os
import random
import select
import shlex
import subprocess

import scap.log
from . import utils
from mozprocess import processhandler

SSH = ('/usr/bin/ssh', '-oBatchMode=yes', '-oSetupTimeout=10', '-F/dev/null')


class Job(object):
    """Execute a job on a group of remote hosts via ssh."""

    def __init__(self, hosts=None, command=None, user=None):
        self.hosts(hosts or [])
        self._command = command
        self._reporter = None
        self._user = user
        self.max_failure = len(hosts)

    def get_logger(self):
        """Lazy getter for a logger instance."""
        return utils.get_logger()

    def hosts(self, hosts):
        """Set hosts to run command on."""
        self._hosts = list(hosts)
        return self

    def role(self, role):
        """Set hosts to run command on by network role."""
        return self.hosts(utils.read_hosts_file(role))

    def shuffle(self):
        """Randomize order of target hosts."""
        random.shuffle(self._hosts)
        return self

    def exclude_hosts(self, exclude):
        exclude = list(exclude)
        self.hosts([host for host in self._hosts if host not in exclude])

    def command(self, command):
        """Set command to run."""
        self._command = command
        return self

    def progress(self, label):
        """Monitor job progress with a :class:`scap.log.ProgressReporter`.

        Use of this method changes the runtime behavior of :meth:`run` to
        return counts of successes and failures instead of a list of results.
        """
        self._reporter = scap.log.ProgressReporter(label)
        return self

    def run(self, batch_size=80):
        """Run the job.

        :returns: List of (host, status, output) tuples or
                  tuple of (success, fail) counts
        :raises: RuntimeError if command has not been set
        """
        if not self._command:
            raise RuntimeError('Command must be provided')

        if not self._hosts:
            self.get_logger().warning(
                'Job %s called with an empty host list.', self._command)
            if self._reporter:
                return (0, 0)
            else:
                return []

        if self._reporter:
            return self._run_with_reporter(batch_size)
        else:
            logger = utils.get_logger()
            failed = 0
            outputs = {}
            for host, status, output in cluster_ssh_threaded(
                self._hosts, self._command, self._user, batch_size,
                self.max_failure):
                if status > 0:
                    failed += 1
                    logger.log("host %s failed: %s" % (host, output))

            return len(outputs)-failed, failed

    def _run_with_reporter(self, batch_size):
        """Run job and feed results to a :class:`scap.log.ProgressReporter` as
        they come in."""
        self._reporter.expect(len(self._hosts))
        self._reporter.start()

        for host, status, output in cluster_ssh(self._hosts, self._command,
                                                self._user, batch_size,
                                                self.max_failure):
            if status == 0:
                self._reporter.add_success()
            else:
                self.get_logger().warning('%s on %s returned [%d]: %s',
                    self._command, host, status, output)
                self._reporter.add_failure()
        self._reporter.finish()
        return self._reporter.ok, self._reporter.failed


def cluster_ssh(hosts, command, user=None, limit=80, max_fail=None):
    """Run a command via SSH on multiple hosts concurrently."""
    hosts = set(hosts)
    # Ensure a minimum batch size of 1
    limit = max(limit, 1)

    max_failure = len(hosts) if max_fail is None else max_fail

    try:
        command = shlex.split(command)
    except AttributeError:
        pass

    failures = 0
    procs = {}
    fds = {}
    poll = select.epoll()
    try:
        while hosts or procs:
            if hosts and len(procs) < limit:
                host = hosts.pop()
                ssh_command = list(SSH)
                if user:
                    ssh_command.append('-l%s' % user)
                ssh_command.append(host)
                ssh_command.extend(command)
                proc = subprocess.Popen(ssh_command, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, preexec_fn=os.setsid)
                procs[proc.pid] = (proc, host)
                poll.register(proc.stdout, select.EPOLLIN)

            elif procs:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except OSError as e:
                    # We lost track of our children somehow. So grab any child
                    # process from procs (they're all dead anyway) and pretend
                    # it exited normally.
                    # See https://bugs.python.org/issue1731717
                    if e.errno == errno.ECHILD:
                        pid = next(iter(procs))
                        status = 0
                    else:
                        raise

                for fd, event in poll.poll(0.01):
                    fds[fd] = fds.get(fd, '') + os.read(fd, 1048576)

                if pid:
                    status = -(status & 255) or (status >> 8)
                    if status != 0:
                        failures = failures + 1
                    proc, host = procs.pop(pid)
                    poll.unregister(proc.stdout)
                    output = fds.pop(proc.stdout.fileno(), '')
                    if failures > max_failure:
                        hosts = []
                    yield host, status, output
    finally:
        poll.close()
        for pid, (proc, host) in procs.items():
            proc.kill()

def cluster_ssh_threaded(hosts, command, user=None, limit=80, max_fail=None):
    """Run a command via SSH on multiple hosts concurrently."""
    hosts = set(hosts)
    # Ensure a minimum batch size of 1
    limit = max(limit, 1)

    max_failure = len(hosts) if max_fail is None else max_fail

    try:
        command = shlex.split(command)
    except AttributeError:
        pass

    procs = {}
    finished = {}
    failures = 0
    try:
        def output_callback(host):
            def print_line(line):
                print "%s: %s\n" % (host, line)

            return print_line

        def finish_callback(cmd):
            def final_callback():
                print "Finished %s" % cmd
                finished[cmd] = procs[cmd]
                del procs[cmd]

            return final_callback

        while failures < max_failure and (hosts or len(procs)):
            if hosts and len(procs) < limit:
                host = hosts.pop()
                ssh_command = list(SSH)
                if user:
                    ssh_command.append('-l%s' % user)
                ssh_command.append(host)
                ssh_command.extend(command)

                cmdkey = " ".join(ssh_command)
                p = processhandler.ProcessHandler(ssh_command,
                    processOutputLine=output_callback(host),
                    onFinish=finish_callback(cmdkey))
                p.host = host
                procs[cmdkey] = p
                p.run()
            elif len(finished):
                (key, ph) = finished.popitem()
                status = ph.poll()
                if status > 0:
                    failures += 1
                host = ph.host
                output = ph.output
                yield host, status, output
            # todo: throw exception if max_failure exceeded?
    finally:
        while len(procs):
            ph = procs.keys()[0]
            procs[ph].kill()


