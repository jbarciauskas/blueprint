import fnmatch
import glob
import logging
import os
import os.path
import re
import subprocess

from blueprint import deps


# The default list of ignore patterns.
#
# XXX Update `blueprintignore`(5) if you make changes here.
IGNORE = ('*.dpkg-*',
          '/etc/.git',
          '/etc/.pwd.lock',
          '/etc/X11/default-display-manager',
          '/etc/alternatives',
          '/etc/apparmor',
          '/etc/apparmor.d',
          '/etc/ca-certificates.conf',
          '/etc/console-setup',
          # TODO Only if it's a symbolic link to ubuntu.
          '/etc/dpkg/origins/default',
          '/etc/fstab',
          '/etc/group-',
          '/etc/group',
          '/etc/gshadow-',
          '/etc/gshadow',
          '/etc/hostname',
          '/etc/init.d/.legacy-bootordering',
          '/etc/initramfs-tools/conf.d/resume',
          '/etc/ld.so.cache',
          '/etc/localtime',
          '/etc/mailcap',
          '/etc/mtab',
          '/etc/modules',
          '/etc/motd',  # TODO Only if it's a symbolic link to /var/run/motd.
          '/etc/network/interfaces',
          '/etc/passwd-',
          '/etc/passwd',
          '/etc/popularity-contest.conf',
          '/etc/prelink.cache',
          '/etc/resolv.conf',  # Most people use the defaults.
          '/etc/rc.d',
          '/etc/rc0.d',
          '/etc/rc1.d',
          '/etc/rc2.d',
          '/etc/rc3.d',
          '/etc/rc4.d',
          '/etc/rc5.d',
          '/etc/rc6.d',
          '/etc/rcS.d',
          '/etc/shadow-',
          '/etc/shadow',
          '/etc/ssh/ssh_host_*_key*',
          '/etc/ssl/certs',
          '/etc/sysconfig/network',
          '/etc/timezone',
          '/etc/udev/rules.d/70-persistent-*.rules')


def _cache_open(pathname, mode):
    f = open(pathname, mode)
    if 'SUDO_UID' in os.environ and 'SUDO_GID' in os.environ:
        uid = int(os.environ['SUDO_UID'])
        gid = int(os.environ['SUDO_GID'])
        os.fchown(f.fileno(), uid, gid)
    return f

def apt_exclusions():
    """
    Return the set of packages that should never appear in a blueprint because
    they're already guaranteed (to some degree) to be there.
    """

    CACHE = '/tmp/blueprint-apt-exclusions'
    OLDCACHE = '/tmp/blueprint-exclusions'

    # Read from a cached copy.  Move the old cache location to the new one
    # if necessary.
    try:
        os.rename(OLDCACHE, CACHE)
    except OSError:
        pass
    try:
        return set([line.rstrip() for line in open(CACHE)])
    except IOError:
        pass
    logging.info('searching for APT packages to exclude')

    # Start with the root packages for the various Ubuntu installations.
    s = set(['grub-pc',
             'installation-report',
             'language-pack-en',
             'language-pack-gnome-en',
             'linux-generic-pae',
             'linux-server',
             'os-prober',
             'ubuntu-desktop',
             'ubuntu-minimal',
             'ubuntu-standard',
             'wireless-crda'])

    # Find the essential and required packages.  Every server's got 'em, no
    # one wants to muddle their blueprint with 'em.
    for field in ('Essential', 'Priority'):
        try:
            p = subprocess.Popen(['dpkg-query',
                                  '-f=${{Package}} ${{{0}}}\n'.format(field),
                                  '-W'],
                                 close_fds=True, stdout=subprocess.PIPE)
        except OSError:
            _cache_open(CACHE, 'w').close()
            return s
        for line in p.stdout:
            try:
                package, property = line.rstrip().split()
                if property in ('yes', 'important', 'required', 'standard'):
                    s.add(package)
            except ValueError:
                pass

    # Walk the dependency tree all the way to the leaves.
    s = deps.apt(s)

    # Write to a cache.
    logging.info('caching excluded APT packages')
    f = _cache_open(CACHE, 'w')
    for package in sorted(s):
        f.write('{0}\n'.format(package))
    f.close()

    return s


def yum_exclusions():
    """
    Return the set of packages that should never appear in a blueprint because
    they're already guaranteed (to some degree) to be there.
    """

    CACHE = '/tmp/blueprint-yum-exclusions'

    # Read from a cached copy.  Move the old cache location to the new one
    # if necessary.
    try:
        return set([line.rstrip() for line in open(CACHE)])
    except IOError:
        pass
    logging.info('searching for Yum packages to exclude')

    # Start with a few groups that install common packages.
    s = set()
    pattern = re.compile(r'^   (\S+)')
    try:
        p = subprocess.Popen(['yum', 'groupinfo',
                              'core','base', 'gnome-desktop'],
                             close_fds=True, stdout=subprocess.PIPE)
    except OSError:
        _cache_open(CACHE, 'w').close()
        return s
    for line in p.stdout:
        match = pattern.match(line)
        if match is None:
            continue
        p2 = subprocess.Popen(['rpm',
                               '-q',
                               '--qf=%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}',
                               match.group(1)],
                              close_fds=True, stdout=subprocess.PIPE)
        stdout, stderr = p2.communicate()
        s.add((match.group(1), stdout))

    # Walk the dependency tree all the way to the leaves.
    s = deps.yum(s)

    # Write to a cache.
    logging.info('caching excluded Yum packages')
    f = _cache_open(CACHE, 'w')
    for package in sorted(s):
        f.write('{0}\n'.format(package))
    f.close()

    return s


# Cache things that are ignored by default first.
_cache = {
    'file': [(pattern, False) for pattern in IGNORE],
    'package': [('apt', package, False) for package in apt_exclusions()] +
                [('yum', package, False) for package in yum_exclusions()],
}

# Cache the patterns stored in the `~/.blueprintignore` file.
try:
    f = open(os.path.expanduser('~/.blueprintignore'))
    logging.info('parsing ~/.blueprintignore')
    for pattern in f:
        pattern = pattern.rstrip()

        # Comments and blank lines.
        if '' == pattern or '#' == pattern[0]:
            continue

        # Negated lines.
        if '!' == pattern[0]:
            pattern = pattern[1:]
            negate = True
        else:
            negate = False

        # Normalize file resources, which don't need the : and type qualifier,
        # into the same format as others, like packages.
        if ':' == pattern[0]:
            try:
                restype, pattern = pattern[1:].split(':', 2)
            except ValueError:
                continue
        else:
            restype = 'file'
        if restype not in _cache:
            continue

        # Ignore or unignore a file, glob, or directory tree.
        if 'file' == restype:
            _cache['file'].append((pattern, negate))

        # Ignore a package and its dependencies or unignore a single package.
        # Empirically, the best balance of power and granularity comes from
        # this arrangement.  Take build-esseantial's mutual dependence with
        # dpkg-dev as an example of why.
        elif 'package' == restype:
            try:
                manager, package = pattern.split('/')
            except ValueError:
                logging.warning('invalid package ignore "{0}"'.format(pattern))
                continue
            _cache['package'].append((manager, package, negate))
            if not negate:
                for dep in getattr(deps, manager, lambda(s): [])(package):
                    _cache['package'].append((manager, dep, negate))

        # Swing and a miss.
        else:
            logging.warning('unrecognized ignore type "{0}"'.format(restype))
            continue

except IOError:
    pass


def file(pathname, ignored=False):
    """
    Return `True` if the `gitignore`(5)-style `~/.blueprintignore` file says
    the given file should be ignored.  The starting state of the file may be
    overridden by setting `ignored` to `True`.
    """

    # Determine if the `pathname` matches the `pattern`.  `filename` is
    # given as a convenience.  See `gitignore`(5) for the rules in play.
    def match(filename, pathname, pattern):
        dir_only = '/' == pattern[-1]
        pattern = pattern.rstrip('/')
        if -1 == pattern.find('/'):
            if fnmatch.fnmatch(filename, pattern):
                return os.path.isdir(pathname) if dir_only else True
        else:
            for p in glob.glob(os.path.join('/etc', pattern)):
                if pathname == p or pathname.startswith('{0}/'.format(p)):
                    return True
        return False

    # Iterate over exclusion rules until a match is found.  Then iterate
    # over inclusion rules that appear later.  If there are no matches,
    # include the file.  If only an exclusion rule matches, exclude the
    # file.  If an inclusion rule also matches, include the file.
    filename = os.path.basename(pathname)
    for pattern, negate in _cache['file']:
        if ignored != negate or not match(filename, pathname, pattern):
            continue
        ignored = not ignored

    return ignored


def package(manager, package, ignored=False):
    """
    Iterate over package exclusion rules looking for exact matches.  As with
    files, search for a negated rule after finding a match.  Return True to
    indicate the package should be ignored.
    """
    for m, p, negate in _cache['package']:
        if ignored != negate or manager != m or package != p and '*' != p:
            continue
        ignored = not ignored
    return ignored
