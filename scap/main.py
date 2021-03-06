# -*- coding: utf-8 -*-
"""
    scap.main
    ~~~~~~~~~~
    Command wrappers for scap tasks

"""
import argparse
import errno
import multiprocessing
import os
import pwd
import socket
import subprocess
import time

from . import arg
from . import cli
from . import log
from . import ssh
from . import targets
from . import tasks
from . import utils


class AbstractSync(cli.Application):
    """Base class for applications that want to sync one or more files from
    the deployment server to the rest of the cluster."""

    soft_errors = False

    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        """Perform a sync operation to the cluster."""
        print(utils.logo())
        self._assert_auth_sock()

        self.include = None

        with utils.lock(self.config['lock_file'], self.arguments.message):
            self._check_sync_flag()
            self._before_cluster_sync()
            self._git_repo()
            self._sync_masters()

            full_target_list = self._get_target_list()

            # Run canary checks
            canaries = [node for node in self._get_canary_list()
                        if node in full_target_list]
            if not self.arguments.force:
                with log.Timer('sync-check-canaries', self.get_stats()):
                    sync_cmd = self._proxy_sync_command()
                    sync_cmd.append(socket.getfqdn())
                    update_canaries = ssh.Job(
                        canaries, user=self.config['ssh_user'])
                    update_canaries.command(sync_cmd)
                    update_canaries.progress(
                        log.reporter(
                            'check-canaries',
                            self.config['fancy_progress']))
                    succeeded, failed = update_canaries.run()
                    if failed:
                        self.get_logger().warning(
                            '%d canaries had sync errors', failed)
                        self.soft_errors = True

                # Needs some time for log errors to happen
                wait_time = self.config['canary_wait_time']
                self.get_logger().info('Waiting for canary traffic...')
                time.sleep(wait_time)
                canary_checks = {
                    'service': self.config['canary_service'],
                    'threshold': self.config['canary_threshold'],
                    'logstash': self.config['logstash_host'],
                    'delay': wait_time,
                    'cores': max(multiprocessing.cpu_count() - 2, 1),
                }

                succeeded, failed = tasks.check_canaries(
                    canaries, **canary_checks)

                if failed:
                    raise RuntimeError(
                        '%d test canaries had check failures '
                        '(rerun with --force to override this check)' % failed)

            # Update proxies
            proxies = [node for node in self._get_proxy_list()
                       if node in full_target_list]
            with log.Timer('sync-proxies', self.get_stats()):
                sync_cmd = self._proxy_sync_command()
                # Proxies should always use the current host as their sync
                # origin server.
                sync_cmd.append(socket.getfqdn())
                update_proxies = ssh.Job(proxies, user=self.config['ssh_user'])
                update_proxies.command(sync_cmd)
                update_proxies.progress(
                    log.reporter(
                        'sync-proxies',
                        self.config['fancy_progress']))
                succeeded, failed = update_proxies.run()
                if failed:
                    self.get_logger().warning(
                        '%d proxies had sync errors', failed)
                    self.soft_errors = True

            # Update apaches
            with log.Timer('sync-apaches', self.get_stats()):
                update_apaches = ssh.Job(
                    full_target_list, user=self.config['ssh_user'])
                update_apaches.exclude_hosts(proxies)
                update_apaches.exclude_hosts([socket.getfqdn()])
                if not self.arguments.force:
                    update_apaches.exclude_hosts(canaries)
                update_apaches.shuffle()
                update_apaches.command(self._apache_sync_command(proxies))
                update_apaches.progress(
                    log.reporter(
                        'sync-apaches',
                        self.config['fancy_progress']))
                succeeded, failed = update_apaches.run()
                if failed:
                    self.get_logger().warning(
                        '%d apaches had sync errors', failed)
                    self.soft_errors = True

            self._after_cluster_sync()

        self._after_lock_release()
        if self.soft_errors:
            return 1
        else:
            return 0

    def _before_cluster_sync(self):
        pass

    def _check_sync_flag(self):
        sync_flag = os.path.join(self.config['stage_dir'], 'sync.flag')
        if os.path.exists(sync_flag):
            stat = os.stat(sync_flag)
            owner = pwd.getpwuid(stat.st_uid).pw_name
            utils.get_logger().error(
                "%s's sync.flag is blocking deployments", owner)
            raise IOError(errno.EPERM, 'Blocked by sync.flag', sync_flag)

    def _get_master_list(self):
        """Get list of deploy master hostnames that should be updated before
        the rest of the cluster."""
        return targets.get('dsh_masters', self.config).all

    def _get_proxy_list(self):
        """Get list of sync proxy hostnames that should be updated before the
        rest of the cluster."""
        return targets.get('dsh_proxies', self.config).all

    def _get_target_list(self):
        """Get list of hostnames that should be updated from the proxies."""
        return list(
            set(self._get_master_list()) |
            set(self._get_proxy_list()) |
            set(targets.get('dsh_targets', self.config).all)
        )

    def _get_api_canary_list(self):
        """Get list of MediaWiki api canaries."""
        return targets.get('dsh_api_canaries', self.config).all

    def _get_app_canary_list(self):
        """Get list of MediaWiki api canaries."""
        return targets.get('dsh_app_canaries', self.config).all

    def _get_canary_list(self):
        """Get list of MediaWiki canary hostnames."""
        return list(
            set(self._get_api_canary_list()) |
            set(self._get_app_canary_list())
        )

    def _sync_masters(self):
        """Sync the staging directory across all deploy master servers."""
        masters = self._get_master_list()
        with log.Timer('sync-masters', self.get_stats()):
            update_masters = ssh.Job(masters, user=self.config['ssh_user'])
            update_masters.exclude_hosts([socket.getfqdn()])
            update_masters.command(self._master_sync_command())
            update_masters.progress(
                log.reporter('sync-masters', self.config['fancy_progress']))
            succeeded, failed = update_masters.run()
            if failed:
                self.get_logger().warning(
                    '%d masters had sync errors', failed)
                self.soft_errors = True

    def _master_sync_command(self):
        """Synchronization command to run on the master hosts."""
        cmd = [self.get_script_path(), 'pull-master']
        if self.verbose:
            cmd.append('--verbose')
        cmd.append(socket.getfqdn())
        return cmd

    def _proxy_sync_command(self):
        """Synchronization command to run on the proxy hosts."""
        cmd = [self.get_script_path(), 'pull', '--no-update-l10n']
        if self.verbose:
            cmd.append('--verbose')
        return cmd

    def _apache_sync_command(self, proxies):
        """
        Synchronization command to run on the apache hosts.

        :param proxies: List of proxy hostnames
        """
        return self._proxy_sync_command() + proxies

    def _git_repo(self):
        """Flatten deploy directory into shared git repo."""
        includes = None

        if self.include is not None:
            includes = []

            parts = self.include.split('/')
            for i in range(1, len(parts)):
                # Include parent directories in sync command or the default
                # exclude will block them and by extension block the target
                # file.
                includes.append('/'.join(parts[:i]))

            includes.append(self.include)

        tasks.sync_common(
            self.config,
            include=includes,
            verbose=self.verbose
        )

        self.get_logger().info('Setting up deploy git directory')
        cmd = '{} deploy-mediawiki -v "{}"'.format(
            self.get_script_path(), self.arguments.message)
        utils.sudo_check_call('mwdeploy', cmd)

    def _after_cluster_sync(self):
        pass

    def _after_lock_release(self):
        pass


@cli.command('security-check')
class SecurityPatchCheck(cli.Application):
    """
    Check if security patches are applied.

    class to check if patches in ``/srv/patches`` have been applied to the
    active wikiversions
    """

    def main(self, *extra_args):
        for version in self.active_wikiversions():
            tasks.check_patch_files(version, self.config)

        return 0


@cli.command('wikiversions-compile', help=argparse.SUPPRESS)
class CompileWikiversions(cli.Application):
    """Compile wikiversions.json to wikiversions.php."""

    def main(self, *extra_args):
        self._run_as('mwdeploy')
        self._assert_current_user('mwdeploy')
        tasks.compile_wikiversions('deploy', self.config)
        return 0


@cli.command('wikiversions-inuse')
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

        print(' '.join(output))
        return 0


@cli.command('cdb-rebuild', help=argparse.SUPPRESS)
class RebuildCdbs(cli.Application):
    """Rebuild localization cache CDB files from the JSON versions."""

    @cli.argument('--version', type=arg.is_version,
                  help='MediaWiki version (eg 1.27.0-wmf.7)')
    @cli.argument('--no-progress', action='store_true', dest='mute',
                  help='Do not show progress indicator.')
    @cli.argument('--staging', action='store_true',
                  help='Rebuild cdb files in staging directory')
    def main(self, *extra_args):
        user = 'mwdeploy'
        source_tree = 'deploy'
        root_dir = self.config['deploy_dir']

        if self.arguments.staging:
            user = 'l10nupdate'
            source_tree = 'stage'
            root_dir = self.config['stage_dir']

        self._run_as(user)
        self._assert_current_user(user)

        # Leave some of the cores free for apache processes
        use_cores = max(multiprocessing.cpu_count() / 2, 1)

        self.versions = self.active_wikiversions(source_tree)

        if self.arguments.version:
            version = self.arguments.version
            if version.startswith('php-'):
                version = version[4:]

            # Assert version is active
            if version not in self.versions:
                raise IOError(
                    errno.ENOENT, 'Version not active', version)

            # Replace dict of active versions with the single version selected
            self.versions = {version: self.versions[version]}

        # Rebuild the CDB files from the JSON versions
        for version, wikidb in self.versions.items():
            cache_dir = os.path.join(
                root_dir, 'php-%s' % version, 'cache', 'l10n')
            tasks.merge_cdb_updates(
                cache_dir, use_cores, True, self.arguments.mute)


@cli.command('sync', help='Deploy MediaWiki to the cluser (formerly scap)')
class Scap(AbstractSync):
    """
    Deploy MediaWiki to the cluster.

    #. Validate php syntax of wmf-config and multiversion
    #. Sync deploy directory on localhost with staging area
    #. Compile wikiversions.json to php in deploy directory
    #. Update l10n files in staging area
    #. Compute git version information
    #. Commit all changes to local git repo in deploy directory
    #. Ask scap masters to sync with current master
    #. Ask scap proxies to sync with master server
    #. Ask apaches to sync with fastest rsync server
    #. Ask apaches to rebuild l10n CDB files
    #. Update wikiversions.php on localhost
    #. Ask apaches to sync wikiversions.php
    #. Restart HHVM across the cluster
    """

    @cli.argument('-r', '--restart', action='store_true', dest='restart',
                  help='Restart HHVM process on target hosts.')
    @cli.argument('--force', action='store_true', help='Skip canary checks')
    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        return super(Scap, self).main(*extra_args)

    def _before_cluster_sync(self):
        self.announce('Started scap: %s', self.arguments.message)

        # Validate php syntax of wmf-config and multiversion
        tasks.check_valid_syntax(
            '%(stage_dir)s/wmf-config' % self.config,
            '%(stage_dir)s/multiversion' % self.config)

        # Bug 63659: Compile deploy_dir/wikiversions.json to cdb
        cmd = '{} wikiversions-compile'.format(self.get_script_path())
        utils.sudo_check_call('mwdeploy', cmd)

        # Update list of extension message files and regenerate the
        # localisation cache.
        with log.Timer('l10n-update', self.get_stats()):
            for version, wikidb in self.active_wikiversions().items():
                tasks.update_localization_cache(
                    version, wikidb, self.verbose, self.config)

        # Compute git version information
        with log.Timer('cache_git_info', self.get_stats()):
            for version, wikidb in self.active_wikiversions().items():
                tasks.cache_git_info(version, self.config)

    def _after_cluster_sync(self):
        target_hosts = self._get_target_list()
        # Ask apaches to rebuild l10n CDB files
        with log.Timer('scap-cdb-rebuild', self.get_stats()):
            rebuild_cdbs = ssh.Job(target_hosts, user=self.config['ssh_user'])
            rebuild_cdbs.shuffle()
            rebuild_cdbs.command(
                'sudo -u mwdeploy -n -- %s cdb-rebuild' %
                self.get_script_path())
            rebuild_cdbs.progress(
                log.reporter(
                    'scap-cdb-rebuild',
                    self.config['fancy_progress']))
            succeeded, failed = rebuild_cdbs.run()
            if failed:
                self.get_logger().warning(
                    '%d hosts had scap-cdb-rebuild errors', failed)
                self.soft_errors = True

        # Update and sync wikiversions.php
        succeeded, failed = tasks.sync_wikiversions(target_hosts, self.config)
        if failed:
            self.get_logger().warning(
                '%d hosts had sync_wikiversions errors', failed)
            self.soft_errors = True

        if self.arguments.restart:
            # Restart HHVM across the cluster
            try:
                succeeded, failed = tasks.restart_hhvm(
                    target_hosts, self.config,
                    # Use a batch size of 5% of the total target list
                    len(target_hosts) // 20)
            except NotImplementedError:
                self.get_logger().warning(
                    "Not restarting HHVM, feature is not implemented")
                return

            if failed:
                self.get_logger().warning(
                    '%d hosts failed to restart HHVM', failed)
                self.soft_errors = True
            self.get_stats().increment('deploy.restart')

    def _after_lock_release(self):
        self.announce(
            'Finished scap: %s (duration: %s)',
            self.arguments.message, utils.human_duration(self.get_duration()))
        self.get_stats().increment('deploy.scap')
        self.get_stats().increment('deploy.all')

    def _handle_keyboard_interrupt(self, ex):
        self.announce(
            'scap aborted: %s (duration: %s)',
            self.arguments.message, utils.human_duration(self.get_duration()))
        return 1

    def _handle_exception(self, ex):
        self.get_logger().warn('Unhandled error:', exc_info=True)
        self.announce(
            'scap failed: %s %s (duration: %s)',
            type(ex).__name__, ex, utils.human_duration(self.get_duration()))
        return 1

    def _before_exit(self, exit_status):
        if self.config:
            self.get_stats().timing('scap.scap', self.get_duration() * 1000)
        return exit_status


@cli.command('pull-master', help=argparse.SUPPRESS)
class SyncMaster(cli.Application):
    """Sync local MediaWiki staging directory with deploy server state."""

    @cli.argument('master', help='Master rsync server to copy from')
    def main(self, *extra_args):
        tasks.sync_master(
            self.config,
            master=self.arguments.master,
            verbose=self.verbose
        )
        return 0


@cli.command('pull', help='Sync local MediaWiki deployment directory with '
                          'deploy server state (formerly sync-common)')
class SyncCommon(cli.Application):
    """Sync local MediaWiki deployment directory with deploy server state."""

    @cli.argument('--no-touch', action='store_false', dest='touch_config',
                  help='Do not touch InitialiseSettings.php after sync.')
    @cli.argument('--no-update-l10n', action='store_false', dest='update_l10n',
                  help='Do not update l10n cache files.')
    @cli.argument('-i', '--include', default=None, action='append',
                  help='Rsync include pattern to limit transfer to.'
                  ' End directories with a trailing `/***`.'
                  ' Can be used multiple times.')
    @cli.argument('servers', nargs=argparse.REMAINDER,
                  help='Rsync server(s) to copy from')
    def main(self, *extra_args):
        tasks.sync_common(
            self.config,
            include=self.arguments.include,
            sync_from=self.arguments.servers,
            verbose=self.verbose,
            touch_config=self.arguments.touch_config
        )
        if self.arguments.update_l10n:
            utils.sudo_check_call(
                'mwdeploy',
                self.get_script_path() + ' cdb-rebuild --no-progress'
            )

        return 0


@cli.command('sync-dir', help=argparse.SUPPRESS)
@cli.command('sync-file')
class SyncFile(AbstractSync):
    """Sync a specific file/directory to the cluster."""

    @cli.argument('--force', action='store_true', help='Skip canary checks')
    @cli.argument('--beta-only-change', action='store_true', dest='beta_only',
                  help='Sync a config file that only affects beta cluster')
    @cli.argument('file', help='File/directory to sync')
    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        return super(SyncFile, self).main(*extra_args)

    def _before_cluster_sync(self):
        # assert file exists
        abspath = os.path.join(
            self.config['stage_dir'], self.arguments.file)
        if not os.path.exists(abspath):
            raise IOError(errno.ENOENT, 'File/directory not found', abspath)
        # Warn when syncing a symlink.
        if os.path.islink(abspath):
            self.get_logger().warning(
                '%s: syncing symlink, not target file contents', abspath)

        if self.arguments.beta_only:
            confroot = os.path.join(
                self.config['stage_dir'], 'wmf-config')
            if (abspath.endswith('.php') and
                    os.path.commonprefix([confroot, abspath]) != confroot):
                raise IOError(
                    errno.EPERM,
                    '--beta-only-change not allowed for PHP files', abspath)

        relpath = os.path.relpath(abspath, self.config['stage_dir'])
        if os.path.isdir(relpath):
            relpath = '%s/***' % relpath
        self.include = relpath
        tasks.check_valid_syntax(abspath)

    def _proxy_sync_command(self):
        cmd = [self.get_script_path(), 'pull', '--no-update-l10n']
        if self.arguments.beta_only:
            cmd.append('--no-touch')

        if '/' in self.include:
            parts = self.include.split('/')
            for i in range(1, len(parts)):
                # Include parent directories in sync command or the default
                # exclude will block them and by extension block the target
                # file.
                cmd.extend(['--include', '/'.join(parts[:i])])

        cmd.extend(['--include', self.include])
        if self.verbose:
            cmd.append('--verbose')
        return cmd

    def _after_lock_release(self):
        self.announce(
            'Synchronized %s: %s (duration: %s)',
            self.arguments.file, self.arguments.message,
            utils.human_duration(self.get_duration()))
        self.get_stats().increment('deploy.sync-file')
        self.get_stats().increment('deploy.all')


@cli.command('sync-l10n')
class SyncL10n(AbstractSync):
    """Sync l10n files for a given branch and rebuild cache files."""

    @cli.argument('--force', action='store_true', help='Skip canary checks')
    @cli.argument('version', type=arg.is_version,
                  help='MediaWiki version (eg 1.27.0-wmf.7)')
    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        return super(SyncL10n, self).main(*extra_args)

    def _before_cluster_sync(self):
        if self.arguments.version.startswith('php-'):
            self.arguments.version = self.arguments.version[4:]

        # Assert version is active
        if self.arguments.version not in self.active_wikiversions():
            raise IOError(
                errno.ENOENT, 'Version not active', self.arguments.version)

        # Assert l10n cache dir for version exists
        abspath = os.path.join(
            self.config['stage_dir'],
            'php-%s/cache/l10n' % self.arguments.version)
        if not os.path.isdir(abspath):
            raise IOError(errno.ENOENT, 'Directory not found', abspath)

        relpath = os.path.relpath(abspath, self.config['stage_dir'])
        self.include = '%s/***' % relpath

    def _proxy_sync_command(self):
        cmd = [
            self.get_script_path(), 'pull', '--no-update-l10n', '--no-touch']

        parts = self.include.split('/')
        for i in range(1, len(parts)):
            # Include parent directories in sync command or the default
            # exclude will block them and by extension block the target
            # file.
            cmd.extend(['--include', '/'.join(parts[:i])])

        cmd.extend(['--include', self.include])
        if self.verbose:
            cmd.append('--verbose')
        return cmd

    def _after_cluster_sync(self):
        # Rebuild l10n CDB files
        target_hosts = self._get_target_list()
        with log.Timer('scap-cdb-rebuild', self.get_stats()):
            rebuild_cdbs = ssh.Job(target_hosts, user=self.config['ssh_user'])
            rebuild_cdbs.shuffle()
            cdb_cmd = 'sudo -u mwdeploy -n -- %s cdb-rebuild --version %s' % (
                      self.get_script_path(),
                      self.arguments.version)
            rebuild_cdbs.command(cdb_cmd)
            rebuild_cdbs.progress(
                log.reporter(
                    'scap-cdb-rebuild',
                    self.config['fancy_progress']))
            succeeded, failed = rebuild_cdbs.run()
            if failed:
                self.get_logger().warning(
                    '%d hosts had scap-cdb-rebuild errors', failed)
                self.soft_errors = True

    def _after_lock_release(self):
        self.announce(
            'scap sync-l10n completed (%s) (duration: %s)',
            self.arguments.version, utils.human_duration(self.get_duration()))
        self.get_stats().increment('l10nupdate-sync')


@cli.command('sync-wikiversions')
class SyncWikiversions(AbstractSync):
    """Rebuild and sync wikiversions.php to the cluster."""

    @cli.argument('--force', action='store_true', help='Skip canary checks')
    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        self._assert_auth_sock()

        # check for the presence of ExtensionMessages and l10n cache
        # for every branch of mediawiki that is referenced in wikiversions.json
        # to avoid syncing a branch that is lacking these critical files.
        for version, wikidb in self.active_wikiversions().items():
            ext_msg = os.path.join(
                self.config['stage_dir'],
                'wmf-config', 'ExtensionMessages-%s.php' % version)
            err_msg = 'ExtensionMessages not found in %s' % ext_msg
            utils.check_file_exists(ext_msg, err_msg)

            cache_file = os.path.join(
                self.config['stage_dir'],
                'php-%s' % version, 'cache', 'l10n', 'l10n_cache-en.cdb')
            err_msg = 'l10n cache missing for %s' % version
            utils.check_file_exists(cache_file, err_msg)

        # this is here for git_repo
        self.include = '/wikiversions*.{json,php}'
        with utils.lock(self.config['lock_file'], self.arguments.message):
            self._check_sync_flag()
            self._git_repo()
            self._sync_masters()
            mw_install_hosts = self._get_target_list()
            tasks.sync_wikiversions(mw_install_hosts, self.config)

        self.announce(
            'rebuilt wikiversions.php and synchronized wikiversions files: %s',
            self.arguments.message)

        self.get_stats().increment('deploy.sync-wikiversions')
        self.get_stats().increment('deploy.all')


@cli.command('l10n-update')
class UpdateL10n(cli.Application):
    """Update localization files."""

    def main(self, *extra_args):
        for version, wikidb in self.active_wikiversions().items():
            tasks.update_localization_cache(
                version, wikidb, self.verbose, self.config)


@cli.command('hhvm-restart')
class RestartHHVM(cli.Application):
    """
    Restart the HHVM fcgi process on the local server.

    #. Depool the server if registered with pybal
    #. Restart HHVM process
    #. Re-pool the server if needed
    """

    def main(self, *extra_args):
        self._run_as('mwdeploy')
        self._assert_current_user('mwdeploy')

        if not utils.is_service_running('hhvm'):
            self.get_logger().debug('HHVM not running')
            return 0

        # Restart HHVM
        try:
            subprocess.check_call('/usr/local/bin/restart-hhvm')
        except subprocess.CalledProcessError:
            self.get_logger().warning(
                'Could not correctly restart the service')
            return 1


@cli.command('hhvm-graceful')
class HHVMGracefulAll(AbstractSync):
    """Perform a rolling restart of HHVM across the cluster."""

    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        exit_code = 0
        self.announce('Restarting HHVM: %s', self.arguments.message)

        target_hosts = self._get_target_list()
        try:
            succeeded, failed = tasks.restart_hhvm(
                target_hosts, self.config,
                # Use a batch size of 5% of the total target list
                len(target_hosts) // 20)
        except NotImplementedError:
            self.get_logger().warning(
                "Not restarting HHVM, feature is not implemented")
            exit_code = 1
        else:
            if failed:
                self.get_logger().warning(
                    '%d hosts failed to restart HHVM', failed)
                self.get_stats().increment('deploy.fail')
                exit_code = 1
        self.announce(
            'Finished HHVM restart: %s (duration: %s)',
            self.arguments.message,
            utils.human_duration(self.get_duration()))
        self.get_stats().increment('deploy.restart')

        return exit_code


@cli.command('cdb-json-refresh', help=argparse.SUPPRESS)
class RefreshCdbJsonFiles(cli.Application):
    """
    Create JSON/MD5 files for all CDB files in a directory.

    This will put a JSON and MD5 file in /upstream for each CDB file.

    This can be combined with rsync and the scap-rebuild-cdbs to
    push out large CDB files with minimal traffic. CDB files change
    drastically with small key/value changes, where as JSON files do not, and
    thus they diff/rdiff much better.

    When pushing updates with rsync, this should be run before running rsync.
    The rsync command should exclude CDB files or at least use
    -ignore-existing. After the rsync is done, scap-rebuild-cdbs can be
    run on each server to apply the updates to the CDB files.
    """

    @cli.argument('-d', '--directory', required=True,
                  type=arg.is_dir,
                  help='Directory containing cdb files')
    @cli.argument('-t', '--threads', default=1, type=int,
                  help='Number of threads to use to build json/md5 files')
    def main(self, *extra_args):
        cdb_dir = os.path.realpath(self.arguments.directory)
        upstream_dir = os.path.join(cdb_dir, 'upstream')
        use_cores = self.arguments.threads

        if not os.path.isdir(cdb_dir):
            raise IOError(errno.ENOENT, 'Directory does not exist', cdb_dir)

        if use_cores < 1:
            use_cores = max(multiprocessing.cpu_count() - 2, 1)

        if not os.path.isdir(upstream_dir):
            os.mkdir(upstream_dir)

        tasks.refresh_cdb_json_files(cdb_dir, use_cores, self.verbose)


@cli.command('log')
@cli.command('sal')
class ServerAdminLog(cli.Application):
    """
    Send an entry to the server admin log
    """

    @cli.argument('message', nargs='*', help='Log message for SAL')
    def main(self, *extra_args):
        self.announce(self.arguments.message)
