import glob
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

from PySide6.QtCore import QProcessEnvironment

IS_WINDOWS = sys.platform == 'win32'
# subprocess refuses creationflags other than 0 outside windows
CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0


def is_facefusion_root(path : str) -> bool:
	return bool(path) and os.path.isfile(os.path.join(path, 'facefusion.py')) and os.path.isdir(os.path.join(path, 'facefusion', 'processors'))


def read_facefusion_version(root : str) -> Optional[str]:
	metadata_path = os.path.join(root, 'facefusion', 'metadata.py')
	try:
		with open(metadata_path, encoding = 'utf-8') as metadata_file:
			for line in metadata_file:
				if "'version'" in line:
					return line.split(':', 1)[1].strip().strip(',').strip("'\"")
	except OSError:
		pass
	return None


def run_git(root : str, args : List[str]) -> Optional[str]:
	try:
		output = subprocess.run([ 'git', '-C', root ] + args, capture_output = True, text = True, timeout = 10, creationflags = CREATE_NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	return output.stdout.strip() if output.returncode == 0 and output.stdout.strip() else None


def read_facefusion_build(root : str) -> Dict[str, str]:
	"""version from metadata.py plus the branch, commit and commit date of a git checkout, empty strings when unknown"""
	build = { 'version': read_facefusion_version(root) or '', 'branch': '', 'commit': '', 'date': '', 'repository': '' }
	if not os.path.isdir(os.path.join(root, '.git')):
		return build
	build['branch'] = run_git(root, [ 'rev-parse', '--abbrev-ref', 'HEAD' ]) or ''
	build['commit'] = run_git(root, [ 'rev-parse', 'HEAD' ]) or ''
	build['date'] = run_git(root, [ 'log', '-1', '--format=%cI' ]) or ''
	remote = run_git(root, [ 'remote', 'get-url', 'origin' ]) or ''
	if 'github.com' in remote:
		build['repository'] = remote.split('github.com', 1)[1].strip(':/').removesuffix('.git')
	return build


def fetch_github_json(path : str) -> Any:
	request = urllib.request.Request('https://api.github.com/' + path, headers = { 'Accept': 'application/vnd.github+json', 'User-Agent': 'StudioFF' })
	with urllib.request.urlopen(request, timeout = 15) as response:
		return json.loads(response.read().decode('utf-8'))


def switch_facefusion_commit(root : str, branch : str, commit : str) -> Dict[str, Any]:
	"""moves a facefusion git checkout to one commit, or to the newest one of its branch when commit is empty. the branch keeps its
	name, so update checks still work, and git refuses when local changes would be overwritten. blocks, so run it off the ui thread"""
	result : Dict[str, Any] = { 'error': '', 'commit': '', 'previous': '', 'requirements_changed': False }

	def git(args : List[str], timeout : int = 300) -> subprocess.CompletedProcess:
		return subprocess.run([ 'git', '-C', root ] + args, capture_output = True, text = True, timeout = timeout, creationflags = CREATE_NO_WINDOW)

	def read_requirements() -> str:
		try:
			with open(os.path.join(root, 'requirements.txt'), encoding = 'utf-8') as file:
				return file.read()
		except OSError:
			return ''

	try:
		result['previous'] = git([ 'rev-parse', 'HEAD' ]).stdout.strip()
		old_requirements = read_requirements()
		# the installer makes a shallow clone, so the commit is fetched first. github serves any commit of the repository
		fetched = git([ 'fetch', '--depth', '1', 'origin', commit or branch ])
		if fetched.returncode != 0:
			result['error'] = (fetched.stderr.strip().splitlines() or [ 'git fetch failed' ])[-1]
			return result
		target = commit or git([ 'rev-parse', 'FETCH_HEAD' ]).stdout.strip()
		switched = git([ 'checkout', '-B', branch, target ] if branch and branch != 'HEAD' else [ 'checkout', '--detach', target ])
		if switched.returncode != 0:
			result['error'] = (switched.stderr.strip().splitlines() or [ 'git checkout failed' ])[-1]
			return result
		result['commit'] = git([ 'rev-parse', 'HEAD' ]).stdout.strip()
		result['requirements_changed'] = read_requirements() != old_requirements
	except (OSError, subprocess.SubprocessError) as exception:
		result['error'] = str(exception)
	return result


def compare_github_commits(repository : str, base : str, head : str) -> Dict[str, Any]:
	"""where head stands against base: identical, ahead (newer), behind (older) or diverged, with how many commits apart"""
	if base == head:
		return { 'status': 'identical', 'ahead': 0, 'behind': 0 }
	compare = fetch_github_json('repos/%s/compare/%s...%s' % (repository, base, head))
	return { 'status': compare.get('status', ''), 'ahead': compare.get('ahead_by', 0), 'behind': compare.get('behind_by', 0) }


def fetch_github_build(repository : str, branch : str, commit : str) -> Dict[str, Any]:
	"""newest release and branch head on github, and how far the local commit is behind it. blocks, so run it off the ui thread"""
	result : Dict[str, Any] = { 'release': '', 'branch': branch, 'commit': '', 'date': '', 'status': '', 'behind': 0 }
	try:
		result['release'] = fetch_github_json('repos/%s/releases/latest' % repository).get('tag_name', '')
	except urllib.error.HTTPError as exception:
		# a repository without releases answers 404, that is not a failure
		if exception.code != 404:
			raise
	if branch and branch != 'HEAD':
		head = fetch_github_json('repos/%s/branches/%s' % (repository, urllib.parse.quote(branch)))['commit']
		result['commit'] = head.get('sha', '')
		result['date'] = head.get('commit', {}).get('committer', {}).get('date', '')
		if commit and result['commit'] and commit != result['commit']:
			compare = fetch_github_json('repos/%s/compare/%s...%s' % (repository, commit, result['commit']))
			# ahead means github has commits the local checkout lacks
			result['status'] = compare.get('status', '')
			result['behind'] = compare.get('ahead_by', 0)
		elif commit:
			result['status'] = 'identical'
	return result


def fetch_latest_release(repository : str) -> Dict[str, str]:
	"""tag, web page, date and source zip of the newest github release, empty when there is none. blocks, so run it off the ui thread"""
	try:
		release = fetch_github_json('repos/%s/releases/latest' % repository)
	except urllib.error.HTTPError as exception:
		if exception.code != 404:
			raise
		return { 'tag': '', 'url': '', 'date': '', 'zip': '' }
	return { 'tag': release.get('tag_name', ''), 'url': release.get('html_url', ''), 'date': release.get('published_at', '') or '', 'zip': release.get('zipball_url', '') or '' }


def is_newer_version(tag : str, current : str) -> bool:
	"""compares every number of two versions like v0.10.1 and 0.2, a tag without numbers is never newer"""
	def numbers(version : str) -> tuple:
		parts = []
		for part in version.strip().lstrip('vV').split('.'):
			digits = ''.join(character for character in part if character.isdigit())
			if not digits:
				break
			parts.append(int(digits))
		return tuple(parts)
	newer, older = numbers(tag), numbers(current)
	# pad so v0.3.0 and 0.3 count as the same version
	size = max(len(newer), len(older))
	return bool(newer) and newer + (0,) * (size - len(newer)) > older + (0,) * (size - len(older))


def read_linux_version() -> str:
	"""the distribution's pretty name from os-release and the kernel version"""
	name = ''
	for path in [ '/etc/os-release', '/usr/lib/os-release' ]:
		try:
			with open(path, encoding = 'utf-8') as release_file:
				for line in release_file:
					if line.startswith('PRETTY_NAME='):
						name = line.split('=', 1)[1].strip().strip('"\'')
			break
		except OSError:
			pass
	return '%s  ·  kernel %s' % (name or platform.system(), platform.release())


def read_os_version() -> str:
	if not IS_WINDOWS:
		return read_linux_version()
	try:
		import winreg
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, 'SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion') as key:
			def value(name : str) -> str:
				try:
					return str(winreg.QueryValueEx(key, name)[0])
				except OSError:
					return ''
			product, build, revision = value('ProductName'), value('CurrentBuild'), value('UBR')
			release = value('DisplayVersion') or value('ReleaseId')
	except (ImportError, OSError):
		return platform.platform()
	# windows 11 still calls itself windows 10 in ProductName, the build number tells them apart
	if build.isdigit() and int(build) >= 22000:
		product = product.replace('Windows 10', 'Windows 11')
	return ' '.join(part for part in [ product, release ] if part) + '  ·  version 10.0.%s%s' % (build, '.' + revision if revision else '')


def env_python(prefix : str) -> str:
	"""the python of a venv or conda environment: <prefix>\\python.exe or <prefix>\\Scripts\\python.exe on windows, <prefix>/bin/python elsewhere"""
	if IS_WINDOWS:
		return os.path.join(prefix, 'python.exe') if os.path.isdir(os.path.join(prefix, 'conda-meta')) else os.path.join(prefix, 'Scripts', 'python.exe')
	return os.path.join(prefix, 'bin', 'python')


def get_env_prefix(python_path : str) -> str:
	python_dir = os.path.dirname(os.path.abspath(python_path))
	if os.path.basename(python_dir).lower() in ('scripts', 'bin'):
		return os.path.dirname(python_dir)
	return python_dir


def is_conda_env(python_path : str) -> bool:
	return os.path.isdir(os.path.join(get_env_prefix(python_path), 'conda-meta'))


def find_conda() -> Optional[str]:
	# CONDA_EXE is set by conda's shell hook, where conda itself is only a shell function
	conda = shutil.which('conda') or shutil.which('conda.bat') or os.environ.get('CONDA_EXE')
	if conda and os.path.isfile(conda):
		return conda
	if IS_WINDOWS:
		bases, executable = [ os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs'), os.path.expanduser('~'), 'C:\\ProgramData' ], os.path.join('condabin', 'conda.bat')
	else:
		bases, executable = [ os.path.expanduser('~'), '/opt' ], os.path.join('bin', 'conda')
	for base in bases:
		for name in [ 'Miniconda3', 'miniconda3', 'Anaconda3', 'anaconda3', 'miniforge3', 'Miniforge3' ]:
			candidate = os.path.join(base, name, executable)
			if os.path.isfile(candidate):
				return candidate
	return None


def find_conda_base() -> Optional[str]:
	conda = find_conda()
	if conda:
		# <base>\condabin\conda.bat or <base>\Library\bin\conda.bat or <base>\Scripts\conda.exe, <base>/bin/conda or <base>/condabin/conda
		path = os.path.dirname(os.path.abspath(conda))
		for _ in range(3):
			if os.path.isdir(os.path.join(path, 'envs')) or os.path.isdir(os.path.join(path, 'conda-meta')):
				return path
			path = os.path.dirname(path)
	return None


def find_python_candidates(facefusion_root : str = '') -> List[str]:
	candidates : List[str] = []

	if facefusion_root:
		if IS_WINDOWS:
			patterns = [ '.venv\\Scripts\\python.exe', 'venv\\Scripts\\python.exe', '.conda\\python.exe', 'env\\python.exe' ]
		else:
			patterns = [ '.venv/bin/python', 'venv/bin/python', '.conda/bin/python', 'env/bin/python' ]
		for pattern in patterns:
			candidates.extend(glob.glob(os.path.join(facefusion_root, pattern)))

	conda_base = find_conda_base()
	if conda_base:
		for env_dir in sorted(glob.glob(os.path.join(conda_base, 'envs', '*'))):
			python_path = env_python(env_dir)
			if os.path.isfile(python_path):
				candidates.append(python_path)

	# environments whose name mentions facefusion go first
	candidates.sort(key = lambda path: 'facefusion' not in path.lower())
	return list(dict.fromkeys(candidates))


# python path and modified time to version, starting python takes up to seconds
PYTHON_VERSIONS : Dict[Tuple[str, float], str] = {}


def probe_python_version(python_path : str) -> Optional[str]:
	try:
		key = (os.path.normcase(os.path.abspath(python_path)), os.path.getmtime(python_path))
		if key in PYTHON_VERSIONS:
			return PYTHON_VERSIONS[key]
		output = subprocess.run([ python_path, '-c', 'import sys; print(sys.version.split()[0])' ], capture_output = True, text = True, timeout = 20, creationflags = CREATE_NO_WINDOW)
	except (OSError, subprocess.SubprocessError):
		return None
	version = output.stdout.strip() or None
	if version:
		PYTHON_VERSIONS[key] = version
	return version


def version_tuple(version : Optional[str]) -> tuple:
	try:
		return tuple(int(part) for part in (version or '').split('.')[:2])
	except ValueError:
		return ()


def create_process_environment(python_path : str) -> QProcessEnvironment:
	environment = QProcessEnvironment.systemEnvironment()
	prefix = get_env_prefix(python_path)
	path_parts = [ prefix, os.path.join(prefix, 'Scripts') ] if IS_WINDOWS else [ os.path.join(prefix, 'bin') ]

	if not IS_WINDOWS:
		# opencv of this app adds its own folder and an empty entry (the working directory) when imported, facefusion gets neither
		library_paths = [ path for path in environment.value('LD_LIBRARY_PATH').split(os.pathsep) if path and not os.path.abspath(path).startswith(sys.prefix + os.sep) ]
		# what facefusion's own setup_linux adds before it re-spawns itself, which ENVIRONMENT_READY below skips
		if is_conda_env(python_path):
			library_paths = [ path for path in [ os.path.join(prefix, 'lib') ] + glob.glob(os.path.join(prefix, 'lib', 'python3*', 'site-packages', 'tensorrt_libs')) if os.path.isdir(path) ] + library_paths
		if library_paths:
			environment.insert('LD_LIBRARY_PATH', os.pathsep.join(library_paths))
		else:
			environment.remove('LD_LIBRARY_PATH')

	if is_conda_env(python_path):
		environment.insert('CONDA_PREFIX', prefix)
		if IS_WINDOWS:
			path_parts += \
			[
				os.path.join(prefix, 'Library', 'mingw-w64', 'bin'),
				os.path.join(prefix, 'Library', 'usr', 'bin'),
				os.path.join(prefix, 'Library', 'bin'),
				os.path.join(prefix, 'Lib'),
				os.path.join(prefix, 'Lib', 'site-packages', 'tensorrt_libs')
			]
	else:
		environment.insert('VIRTUAL_ENV', prefix)

	path_parts = [ path for path in path_parts if os.path.isdir(path) ]
	environment.insert('PATH', os.pathsep.join(path_parts + [ environment.value('PATH') ]))
	# skip facefusion's self re-spawn so stop() can terminate a single process tree
	environment.insert('ENVIRONMENT_READY', '1')
	environment.insert('OMP_NUM_THREADS', '1')
	if not IS_WINDOWS:
		environment.insert('MALLOC_ARENA_MAX', '4')
	environment.insert('PYTHONUNBUFFERED', '1')
	environment.insert('PYTHONIOENCODING', 'utf-8')
	environment.insert('COLUMNS', '200')
	return environment


def environment_to_dict(environment : QProcessEnvironment) -> Dict[str, str]:
	return { key: environment.value(key) for key in environment.keys() }


def find_child_processes(pid : int) -> List[int]:
	"""every process below pid, read from /proc"""
	children : Dict[int, List[int]] = {}
	for entry in os.listdir('/proc'):
		if entry.isdigit():
			try:
				with open('/proc/%s/stat' % entry, 'rb') as stat_file:
					# the name in brackets can hold spaces, the parent id is the second field after it
					parent = int(stat_file.read().rsplit(b')', 1)[1].split()[1])
			except (OSError, IndexError, ValueError):
				continue
			children.setdefault(parent, []).append(int(entry))
	found : List[int] = []
	pending = [ pid ]
	while pending:
		for child in children.get(pending.pop(), []):
			found.append(child)
			pending.append(child)
	return found


def kill_process_tree(pid : int) -> None:
	if not pid:
		return
	if IS_WINDOWS:
		subprocess.run([ 'taskkill', '/PID', str(pid), '/T', '/F' ], capture_output = True, creationflags = CREATE_NO_WINDOW)
		return
	# the whole tree like taskkill /T /F, the parent first so it cannot start new children meanwhile
	for process_id in [ pid ] + find_child_processes(pid):
		try:
			os.kill(process_id, signal.SIGKILL)
		except OSError:
			pass


def reveal_in_file_manager(path : str) -> None:
	"""opens the folder of path with path picked, in explorer or in the file manager of the desktop"""
	if IS_WINDOWS:
		subprocess.Popen([ 'explorer', '/select,', path ])
		return
	from PySide6.QtCore import QUrl
	from PySide6.QtDBus import QDBusConnection, QDBusMessage
	from PySide6.QtGui import QDesktopServices

	# most linux file managers (dolphin, nautilus, nemo, thunar) can pick a file through the FileManager1 interface
	message = QDBusMessage.createMethodCall('org.freedesktop.FileManager1', '/org/freedesktop/FileManager1', 'org.freedesktop.FileManager1', 'ShowItems')
	message.setArguments([ [ QUrl.fromLocalFile(path).toEncoded().data().decode() ], '' ])
	reply = QDBusConnection.sessionBus().call(message, timeout = 2000)
	if reply.type() == QDBusMessage.MessageType.ErrorMessage:
		QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))


def has_nvidia_gpu() -> bool:
	return bool(shutil.which('nvidia-smi'))


def check_dependencies() -> Dict[str, bool]:
	return { name: bool(shutil.which(name)) for name in [ 'git', 'ffmpeg', 'ffprobe', 'curl' ] }
