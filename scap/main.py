# -*- coding: utf-8 -*-
"""
    scap.main
    ~~~~~~~~~~
    Command wrappers for scap tasks

"""
import argparse
import os
import subprocess
import time

from . import cli
from . import log
from . import ssh
from . import tasks
from . import utils


class MWVersionsInUse(cli.Application):
    """Get a list of the active MediaWiki versions."""

    @cli.argument('--withdb', action='store_true',
        help='Add `=wikidb` with some wiki using the version.')
    def main(self, *extra_args):
        versions = self.active_wikiversions()

        if self.arguments.withdb:
            output = ['%s=%s' % (version, wikidb)
                    for version, wikidb in versions.items()]
        else:
            output = [str(version) for version in versions.keys()]

        print ' '.join(output)
        return 0

    def _process_arguments(self, args, extra_args):
        """Log warnings about unexpected arguments but don't exit."""
        if extra_args:
            self.logger.warning(
                'Unexpected argument(s) ignored: %s', extra_args)

        return args, extra_args


class SyncCommon(cli.Application):
    """Sync local MediaWiki deployment directory with deploy server state."""

    @cli.argument('servers', nargs=argparse.REMAINDER,
        help='Rsync server(s) to copy from')
    def main(self, *extra_args):
        tasks.sync_common(self.config, self.arguments.servers)
        return 0


class SyncWikiversions(cli.Application):
    """Rebuild and sync wikiversions.cdb to the cluster."""

    def _process_arguments(self, args, extra_args):
        args.message = ' '.join(args.message) or '(no message)'
        return args, extra_args

    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        assert 'SSH_AUTH_SOCK' in os.environ, \
            '%s requires SSH agent forwarding' % self.program_name

        mw_install_hosts = utils.read_dsh_hosts_file('mediawiki-installation')
        tasks.sync_wikiversions(mw_install_hosts, self.config)

        self.announce(
            'rebuilt wikiversions.cdb and synchronized wikiversions files: %s',
            self.arguments.message)


class Scap(cli.Application):
    """Deploy MediaWiki to the cluster."""

    def __init__(self, exe_name):
        super(self.__class__, self).__init__(exe_name)
        self.start = time.time()

    def _process_arguments(self, args, extra_args):
        args.message = ' '.join(args.message) or '(no message)'
        return args, extra_args

    @cli.argument('message', nargs=argparse.REMAINDER,
        help='Log message for SAL')
    def main(self, *extra_args):
        """Core business logic of scap process.

        1. Validate php syntax of wmf-config and multiversion
        2. Sync deploy directory on localhost with staging area
        3. Update l10n files in staging area
        4. Ask scap proxies to sync with master server
        5. Ask apaches to sync with fastest rsync server
        6. Ask apaches to rebuild l10n CDB files
        7. Update wikiversions.cdb on localhost
        8. Ask apaches to sync wikiversions.cdb
        """
        assert 'SSH_AUTH_SOCK' in os.environ, \
            'scap requires SSH agent forwarding'

        with utils.lock('/var/lock/scap'):
            self.announce('Started scap: %s', self.arguments.message)

            tasks.check_php_syntax(
                '%(stage_dir)s/wmf-config' % self.config,
                '%(stage_dir)s/multiversion' % self.config)

            # Update the current machine so that serialization works. Push
            # wikiversions.json changes so mwversionsinuse, set-group-write,
            # and mwscript work with the right version of the files.
            tasks.sync_common(self.config)

            # Update list of extension message files and regenerate the
            # localisation cache.
            with log.Timer('mw-update-l10n', self.stats):
                subprocess.check_call('/usr/local/bin/mw-update-l10n')

            # Update rsync proxies
            scap_proxies = utils.read_dsh_hosts_file('scap-proxies')
            with log.Timer('sync-common to proxies', self.stats):
                update_proxies = ssh.Job(scap_proxies)
                update_proxies.command('/usr/local/bin/sync-common')
                update_proxies.progress('sync-common').run()

            # Update apaches
            mw_install_hosts = utils.read_dsh_hosts_file(
                'mediawiki-installation')
            with log.Timer('update apaches', self.stats) as t:
                update_apaches = ssh.Job(mw_install_hosts)
                update_apaches.exclude_hosts(scap_proxies)
                update_apaches.shuffle()
                update_apaches.command(
                    ['/usr/local/bin/sync-common'] + scap_proxies)
                update_apaches.progress('sync-common').run()
                t.mark('sync-common to apaches')

                rebuild_cdbs = ssh.Job(mw_install_hosts)
                rebuild_cdbs.command('/usr/local/bin/scap-rebuild-cdbs')
                rebuild_cdbs.progress('scap-rebuild-cdbs').run()
                t.mark('scap-rebuild-cdbs')

            # Update and sync wikiversions.cdb
            tasks.sync_wikiversions(mw_install_hosts, self.config)

        self.announce('Finished scap: %s (duration: %s)',
            self.arguments.message, self.human_duration)
        return 0

    @property
    def duration(self):
        """Get the elapsed duration in seconds."""
        return time.time() - self.start

    @property
    def human_duration(self):
        """Get the elapsed duration in human readable form."""
        return utils.human_duration(self.duration)

    def _handle_keyboard_interrupt(self, ex):
        self.announce('scap aborted: %s (duration: %s)',
            self.arguments.message, self.human_duration)
        return 1

    def _handle_exception(self, ex):
        self.logger.debug('Unhandled error:', exc_info=True)
        self.announce('scap failed: %s %s (duration: %s)',
            type(ex).__name__, ex, self.human_duration)
        return 1

    def _before_exit(self, exit_status):
        if self.config:
            self.stats.increment('scap.scap')
            self.stats.timing('scap.scap', self.duration * 1000)
