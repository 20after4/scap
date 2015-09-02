# -*- coding: utf-8 -*-
"""
    scap.utils
    ~~~~~~~~~~
    Contains misc utility functions.

"""
import contextlib
import errno
import fcntl
import hashlib
import json
import logging
import os
import pwd
import random
import re
import socket
import struct
import subprocess
import tempfile
import inspect

from . import ansi
from datetime import datetime


class LockFailedError(Exception):
    """Signal that a locking attempt failed."""
    pass


def find_nearest_host(hosts, port=22, timeout=1):
    """Given a collection of hosts, find the one that is the fewest
    number of hops away.

    >>> # Begin test fixture
    >>> import socket
    >>> fixture_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    >>> fixture_socket.bind(('127.0.0.1', 0))
    >>> fixture_socket.listen(1)
    >>> fixture_port = fixture_socket.getsockname()[1]
    >>> # End test fixture
    >>> find_nearest_host(['127.0.0.1'], port=fixture_port)
    '127.0.0.1'

    :param hosts: Hosts to check
    :param port: Port to try to connect on (default: 22)
    :param timeout: Timeout in seconds (default: 1)
    """
    host_map = {}
    for host in hosts:
        try:
            host_map[host] = socket.getaddrinfo(host, port)[0]
        except socket.gaierror:
            continue

    for ttl in range(1, 30):
        if not host_map:
            break
        for host, info in random.sample(host_map.items(), len(host_map)):
            family, type, proto, _, addr = info
            s = socket.socket(family, type, proto)
            s.setsockopt(socket.IPPROTO_IP, socket.IP_TTL,
                         struct.pack('I', ttl))
            s.settimeout(timeout)
            try:
                s.connect(addr)
            except socket.error as e:
                if e.errno != errno.EHOSTUNREACH:
                    del host_map[host]
                continue
            except socket.timeout:
                continue
            else:
                return host
            finally:
                s.close()


def get_real_username():
    """Get the username of the real user."""
    try:
        # Get the username of the user owning the terminal (ie the user
        # that is running scap even if they are sudo-ing something)
        return os.getlogin()
    except OSError:
        # When running under Jenkins there is no terminal so os.getlogin()
        # blows up. Use the username matching the effective user id
        # instead.
        return get_username()


def get_realm_specific_filename(filename, realm, datacenter):
    """Find the most specific file for the given realm and datacenter.

    The extension is separated from the filename and then recombined with the
    realm and datacenter:
    - base-realm-datacenter.ext
    - base-realm.ext
    - base-datacenter.ext
    """
    base, ext = os.path.splitext(filename)

    if ext == '':
        return filename

    parts = {
        'base': base,
        'realm': realm,
        'datacenter': datacenter,
        'ext': ext,
    }

    possible = (
        '%(base)s-%(realm)s-%(datacenter)%(ext)s',
        '%(base)s-%(realm)s%(ext)s',
        '%(base)s-%(datacenter)%(ext)s',
    )

    for new_filename in (p % parts for p in possible):
        if os.path.isfile(new_filename):
            return new_filename

    # If all else fails, return the original filename
    return filename


def get_username():
    """Get the username of the effective user."""
    return pwd.getpwuid(os.getuid())[0]


def get_disclosable_head(repo_directory):
    """Get the SHA1 of the most recent commit that can be publicly disclosed.
    If a commit only exists locally, it is considered private. This function
    will try to get the tip of the remote tracking branch, and fall back to
    the common ancestor of HEAD and origin."""
    with open(os.devnull, 'wb') as dev_null:
        try:
            return subprocess.check_output(
                ('/usr/bin/git', 'rev-list', '-1', '@{upstream}'),
                cwd=repo_directory, stderr=dev_null).strip()
        except subprocess.CalledProcessError:
            remote = subprocess.check_output(
                ('/usr/bin/git', 'remote'),
                cwd=repo_directory, stderr=dev_null).strip()
            return subprocess.check_output(
                ('/usr/bin/git', 'merge-base', 'HEAD', remote),
                cwd=repo_directory, stderr=dev_null).strip()


def git_info(directory):
    """Compute git version information for a given directory that is
    compatible with MediaWiki's GitInfo class.

    :param directory: Directory to scan for git information
    :returns: Dict of information about current repository state
    """
    git_dir = os.path.join(directory, '.git')
    if not os.path.exists(git_dir):
        raise IOError(errno.ENOENT, '.git not found', directory)

    if os.path.isfile(git_dir):
        # submodules
        with open(git_dir, 'r') as f:
            git_ref = f.read().strip()

        if not git_ref.startswith('gitdir: '):
            raise IOError(errno.EINVAL, 'Unexpected .git contents', git_dir)
        git_ref = git_ref[8:]
        if git_ref[0] != '/':
            git_ref = os.path.abspath(os.path.join(directory, git_ref))
        git_dir = git_ref

    head_file = os.path.join(git_dir, 'HEAD')
    with open(head_file, 'r') as f:
        head = f.read().strip()
    if head.startswith('ref: '):
        head = head[5:]

    head_sha1 = get_disclosable_head(directory)
    commit_date = subprocess.check_output(
        ('/usr/bin/git', 'show', '-s', '--format=%ct', head_sha1),
        cwd=git_dir).strip()

    if head.startswith('refs/heads/'):
        branch = head[11:]
    else:
        branch = head

    # Requires git v1.7.5+
    remote_url = subprocess.check_output(
        ('/usr/bin/git', 'ls-remote', '--get-url'),
        cwd=git_dir).strip()

    return {
        '@directory': directory,
        'head': head,
        'headSHA1': head_sha1,
        'headCommitDate': commit_date,
        'branch': branch,
        'remoteURL': remote_url,
    }


def git_info_filename(directory, install_path, cache_path):
    """Compute the path for a git_info cache file related to a given
    directory.

    >>> git_info_filename('foo', 'foo', '')
    'info.json'
    >>> git_info_filename('foo/bar/baz', 'foo', 'xyzzy')
    'xyzzy/info-bar-baz.json'
    """
    path = directory
    if path.startswith(install_path):
        path = path[len(install_path):]
    return os.path.join(cache_path, 'info%s.json' % path.replace('/', '-'))


def human_duration(elapsed):
    """Format an elapsed seconds count as human readable duration.

    >>> human_duration(1)
    '00m 01s'
    >>> human_duration(65)
    '01m 05s'
    >>> human_duration(60*30+11)
    '30m 11s'
    """
    return '%02dm %02ds' % divmod(elapsed, 60)


def iterate_subdirectories(root):
    """Generator over the child directories of a given directory."""
    for name in os.listdir(root):
        subdir = os.path.join(root, name)
        if os.path.isdir(subdir):
            yield subdir


@contextlib.contextmanager
def lock(filename):
    """Context manager. Acquires a file lock on entry, releases on exit.

    :param filename: File to lock
    :raises: LockFailedError on failure
    """
    lock_fd = None
    try:
        lock_fd = open(filename, 'w+')
        fcntl.lockf(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError as e:
        raise LockFailedError('Failed to lock %s: %s' % (filename, e))
    else:
        yield
    finally:
        if lock_fd:
            fcntl.lockf(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.unlink(filename)


@contextlib.contextmanager
def cd(dirname):
    """Context manager. Cds to dirname, moves back to previous dir on context exit

    :param dirname: directory into which it should change
    """
    old_path = os.getcwd()
    try:
        os.chdir(dirname)
        yield
    finally:
        os.chdir(old_path)


def md5_file(path):
    """Compute the md5 checksum of a file's contents.

    :param path: Path to file
    :returns: hexdigest of md5 checksum
    """
    crc = hashlib.md5()
    with open(path, 'rb') as f:
        # Digest file in 1M chunks just in case it's huge
        for block in iter(lambda: f.read(1048576), b''):
            crc.update(block)
    return crc.hexdigest()


def read_dsh_hosts_file(path):
    """Reads hosts from a file into a list.

    Blank lines and comments are ignored.
    """
    try:
        with open(os.path.join('/etc/dsh/group', path)) as hosts_file:
            return re.findall(r'^[\w\.\-]+', hosts_file.read(), re.MULTILINE)
    except IOError as e:
        raise IOError(e.errno, e.strerror, path)


def sudo_check_call(user, cmd, logger=None):
    """Run a command as a specific user. Reports stdout/stderr of process
    to logger during execution.

    :param user: User to run command as
    :param cmd: Command to execute
    :param logger: Logger to send process output to
    :raises: subprocess.CalledProcessError on non-zero process exit
    """
    if logger is None:
        logger = logging.getLogger('sudo_check_call')

    proc = subprocess.Popen('sudo -u %s -n -- %s' % (user, cmd),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, shell=True)

    output = []

    while proc.poll() is None:
        line = proc.stdout.readline().strip()
        if line:
            output.append(line)
            logger.debug(line)

    output = "\n".join(output)

    if proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output)

    return output


def check_valid_json_file(path):
    if not path.endswith('.json'):
        return
    with open(path) as f:
        try:
            json.load(f)
        except ValueError:
            raise ValueError(
                '%s is an invalid JSON file'
                % path
            )


def check_file_exists(path, message=False):
    if not os.path.isfile(path):
        raise IOError(
            errno.ENOENT,
            message or 'Error: %s is not a file.' % path,
            path
        )


def check_dir_exists(path, message=False):
    if not os.path.isdir(path):
        raise IOError(
            errno.ENOTDIR,
            message or 'Error: %s is not a directory.' % path,
            path
        )


def check_php_opening_tag(path):
    """Checks a PHP file to make sure nothing is before the opening <?php
    except for shebangs.

    :param path: Location of file
    :raises: ValueError on invalid file
    """
    if not path.endswith(('.php', '.inc', '.phtml', '.php5')):
        return
    with open(path) as f:
        text = f.read()

        # Empty files are ok
        if len(text) < 1:
            return

        # Best case scenario to begin with the php open tag
        if text.startswith('<?php'):
            return

        # Also reasonable to start with a doctype declaration
        if text.startswith('<!DOCTYPE'):
            return

        # If the first line is a shebang and the
        # second has <?php, that's ok
        lines = text.splitlines()

        if (
            len(lines) > 1
            and lines[0].startswith('#!')
            and lines[1].startswith('<?php')
        ):
            return

        # None of the return conditions matched, the file must contain <?php
        # but with some content preceeding it.
        raise ValueError(
            '%s has content before opening <?php tag'
            % path
        )


def logo(color=True, **colors):
    """Get the scap logo::

               ___ ____
             ⎛   ⎛ ,----
              \  //==--'
         _//| .·//==--'    ____________________________
        _OO≣=-  ︶ ᴹw ⎞_§ ______  ___\ ___\ ,\__ \/ __ \
       (∞)_, )  (     |  ______/__  \/ /__ / /_/ / /_/ /
         ¨--¨|| |- (  / _______\____/\___/ \__^_/  .__/
             ««_/  «_/ jgs/bd808               /_/

    Ascii art derived from original work by Joan Stark [#]_ and the `speed`
    figlet font [#]_.

    :param color: Color logo using ANSI escapes
    :param colors: Alternate colors
    :returns: str

    .. [#] http://www.oocities.org/spunk1111/farm.htm#pig
    .. [#] http://www.jave.de/figlet/fonts/details/speed.html
    """
    pallet = {
        'pig': ansi.reset() + ansi.esc(ansi.FG_MAGENTA, ansi.BRIGHT),
        'nose': ansi.reset() + ansi.esc(ansi.FG_MAGENTA, ansi.BRIGHT),
        'mouth': ansi.reset() + ansi.esc(ansi.FG_MAGENTA, ansi.BRIGHT),
        'goggles': ansi.reset() + ansi.esc(ansi.FG_YELLOW),
        'brand': ansi.reset(),
        'hoof': ansi.reset() + ansi.esc(ansi.FG_BLUE),
        'wing': ansi.reset() + ansi.esc(ansi.FG_CYAN),
        'speed': ansi.reset() + ansi.esc(ansi.FG_WHITE),
        'text': ansi.reset() + ansi.esc(ansi.FG_GREEN),
        'signature': ansi.reset() + ansi.esc(ansi.FG_BLUE),
        'reset': ansi.reset(),
    }
    pallet.update(colors)

    if not color:
        for key in pallet.keys():
            pallet[key] = ''

    return ''.join(line % pallet for line in [
        '''           %(wing)s___%(reset)s %(wing)s____%(reset)s\n''',
        '''         %(wing)s⎛   ⎛ ,----%(reset)s\n''',
        '''          %(wing)s\  //==--'%(reset)s\n''',
        '''     %(pig)s_//|,.·%(wing)s//==--'%(reset)s    ''',
        '''%(speed)s______%(text)s____''',
        '''%(speed)s_%(text)s____''',
        '''%(speed)s___%(text)s____''',
        '''%(speed)s__%(text)s____%(reset)s\n''',

        '''    %(pig)s_%(goggles)sOO≣=-%(pig)s ''',
        ''' %(wing)s︶%(pig)s %(brand)sᴹw%(pig)s ⎞_§%(reset)s ''',
        '''%(speed)s______%(text)s  ___\ ___\ ,\__ \/ __ \%(reset)s\n''',
        '''   %(pig)s(%(nose)s∞%(pig)s)%(mouth)s_,''',
        '''%(pig)s )  (     |%(reset)s''',
        '''  %(speed)s______%(text)s/__  \/ /__ / /_/ / /_/ /%(reset)s\n''',
        '''     %(pig)s¨--¨|| |- (  /%(reset)s''',
        ''' %(speed)s______%(text)s\____/ \___/ \__^_/  .__/%(reset)s\n''',
        '''         %(hoof)s««%(pig)s_/%(reset)s''',
        '''  %(hoof)s«%(pig)s_/%(reset)s''',
        ''' %(signature)sjgs/bd808%(reset)s''',
        '''                %(text)s/_/%(reset)s\n''',
    ])


@contextlib.contextmanager
def sudo_temp_dir(owner, prefix):
    """Create a temporary directory and delete it after the block.

    :param owner: Directory owner
    :param prefix: Temp directory prefix
    :returns: Full path to temporary directory
    """
    logger = logging.getLogger('sudo_temp_dir')

    while True:
        dirname = os.path.join(tempfile.gettempdir(),
            prefix + str(random.SystemRandom().randint(0, 0xffffffff)))
        if not os.path.exists(dirname):
            break
    # Yes, there is a small race condition here in theory. In practice it
    # should be pretty hard to hit due to scap's global concurrency lock.
    sudo_check_call(owner, 'mkdir "%s"' % dirname, logger)

    try:
        yield dirname
    finally:
        sudo_check_call(owner,
            'find "%s" -maxdepth 1 -delete' % dirname, logger)


def read_pid(path):
    """Read a PID from a file"""
    try:
        return int(open(path).read().strip())
    except IOError as e:
        raise IOError(e.errno, e.strerror, path)


def inside_git_dir(func):
    """Decorator to determine if the "location" argument is a git directory"""
    def wrapper(*args, **kwargs):
        argspec = inspect.getargspec(func)
        try:
            l = args[argspec.args.index('location')]
        except IndexError:
            l = None

        if l is None and argspec.defaults is not None:
            d = dict(zip(
                argspec.args[-len(argspec.defaults):],
                argspec.defaults
            ))
            l = kwargs.get('location', d.get('location'))

        if l is None or not is_git_dir(l):
            raise IOError(errno.ENOENT, 'Location is not a git repo', l)

        return func(*args, **kwargs)
    return wrapper


def is_git_dir(path):
    """Checks if path is a git, doesn't count submodule directories"""
    git_path = os.path.join(
        os.path.abspath(os.path.expandvars(os.path.expanduser(path))),
        '.git'
    )
    return (os.path.isdir(git_path) and
            os.path.isdir(os.path.join(git_path, 'objects')) and
            os.path.isdir(os.path.join(git_path, 'refs')) and
            os.path.isfile(os.path.join(git_path, 'HEAD')))


@inside_git_dir
def git_sha(location, rev='HEAD'):
    """Returns SHA1 for things like HEAD or HEAD~~"""
    with cd(location):
        cmd = '/usr/bin/git rev-parse --verify {}'.format(rev)
        return subprocess.check_output(cmd, shell=True).strip()


@inside_git_dir
def generate_json_tag(location, user=get_real_username()):
    """Generates a json object"""
    timestamp = datetime.utcnow()
    date = timestamp.strftime('%Y-%m-%d')
    cmd = '/usr/bin/git tag --list scap/sync/{}/*'.format(date)
    seq = subprocess.check_output(cmd, shell=True).strip().count('\n') + 1
    tag = 'scap/sync/{0}/{1:04d}'.format(date, seq)
    tag_info = {
        'tag': tag,
        'timestamp': timestamp.isoformat(),
        'user': user,
    }

    return (tag, tag_info)
