"""
Note: this code will actually run as a plugin in the RobotFramework Language
Server, not in the Robocorp Code environment, so, we need to be careful on the
imports so that we only import what's actually available there!

i.e.: We can import `robocorp_ls_core`, and even `robotframework_ls`, but we
can't import `robocorp_code` without some additional work.
"""
import os.path
import sys
from collections import namedtuple

try:
    from robocorp_code.rcc import Rcc  # noqa
except:
    # Automatically add it to the path if executing as a plugin the first time.
    sys.path.append(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    )
    import robocorp_code  # @UnusedImport

    from robocorp_code.rcc import Rcc  # noqa


from typing import Optional, Dict, List, Tuple

from robocorp_ls_core.basic import implements
from robocorp_ls_core.pluginmanager import PluginManager
from robotframework_ls.ep_resolve_interpreter import (
    EPResolveInterpreter,
    IInterpreterInfo,
)
from robocorp_ls_core import uris
from robocorp_ls_core.robotframework_log import get_logger
from pathlib import Path
import weakref

log = get_logger(__name__)


_CachedFileMTimeInfo = namedtuple("_CachedFileMTimeInfo", "st_mtime, st_size, path")

_CachedInterpreterMTime = Tuple[_CachedFileMTimeInfo, Optional[_CachedFileMTimeInfo]]


def _get_mtime_cache_info(file_path: Path) -> _CachedFileMTimeInfo:
    """
    Cache based on the time/size of a given path.
    """
    stat = file_path.stat()
    return _CachedFileMTimeInfo(stat.st_mtime, stat.st_size, str(file_path))


class _CachedFileInfo(object):
    def __init__(self, file_path: Path):
        self.file_path = file_path
        self.mtime_info: _CachedFileMTimeInfo = _get_mtime_cache_info(file_path)
        self.contents: str = file_path.read_text(encoding="utf-8", errors="replace")
        self._yaml_contents: Optional[dict] = None

    @property
    def yaml_contents(self) -> dict:
        yaml_contents = self._yaml_contents
        if yaml_contents is None:
            from robocorp_ls_core import yaml_wrapper
            from io import StringIO

            s = StringIO(self.contents)
            yaml_contents = self._yaml_contents = yaml_wrapper.load(s)
        return yaml_contents

    def is_cache_valid(self) -> bool:
        return self.mtime_info == _get_mtime_cache_info(self.file_path)


class _CachedInterpreterInfo(object):
    def __init__(
        self,
        robot_yaml_file_info: _CachedFileInfo,
        conda_config_file_info: Optional[_CachedFileInfo],
        pm: PluginManager,
    ):
        from robotframework_ls.ep_resolve_interpreter import DefaultInterpreterInfo
        from robotframework_ls.ep_providers import EPConfigurationProvider
        import json

        self._mtime: _CachedInterpreterMTime = self._obtain_mtime(
            robot_yaml_file_info, conda_config_file_info
        )

        configuration_provider: EPConfigurationProvider = pm[EPConfigurationProvider]
        rcc = Rcc(configuration_provider)
        interpreter_id = str(robot_yaml_file_info.file_path)
        result = rcc.run_python_code_robot_yaml(
            """
if __name__ == "__main__":
    import sys
    import json
    import os
    import time

    info = {
        "python_executable": sys.executable,
        "python_environ": dict(os.environ),
    }
    json_contents = json.dumps(info, indent=4)
    sys.stdout.write('JSON START>>')
    sys.stdout.write(json_contents)
    sys.stdout.write('<<JSON END')
    sys.stdout.flush()
    time.sleep(.5)
""",
            conda_config_file_info.contents
            if conda_config_file_info is not None
            else None,
        )
        if not result.success:
            raise RuntimeError(f"Unable to get env details. Error: {result.message}.")

        json_contents: Optional[str] = result.result
        if not json_contents:
            raise RuntimeError(f"Unable to get output when getting environment.")

        start = json_contents.find("JSON START>>")
        end = json_contents.find("<<JSON END")
        if start == -1 or end == -1:
            raise RuntimeError(
                f"Unable to find JSON START>> or <<JSON END in: {json_contents}."
            )

        start += len("JSON START>>")
        json_contents = json_contents[start:end]
        try:
            json_dict: dict = json.loads(json_contents)
        except:
            raise RuntimeError(f"Error loading json: {json_contents}.")

        root = str(robot_yaml_file_info.file_path.parent)

        pythonpath_lst = robot_yaml_file_info.yaml_contents.get("PYTHONPATH", [])
        additional_pythonpath_entries: List[str] = []
        if isinstance(pythonpath_lst, list):
            for v in pythonpath_lst:
                additional_pythonpath_entries.append(os.path.join(root, str(v)))

        self.info: IInterpreterInfo = DefaultInterpreterInfo(
            interpreter_id,
            json_dict["python_executable"],
            json_dict["python_environ"],
            additional_pythonpath_entries,
        )

    def _obtain_mtime(
        self,
        robot_yaml_file_info: _CachedFileInfo,
        conda_config_file_info: Optional[_CachedFileInfo],
    ) -> _CachedInterpreterMTime:
        return (
            robot_yaml_file_info.mtime_info,
            conda_config_file_info.mtime_info if conda_config_file_info else None,
        )

    def is_cache_valid(
        self,
        robot_yaml_file_info: _CachedFileInfo,
        conda_config_file_info: Optional[_CachedFileInfo],
    ) -> bool:
        return self._mtime == self._obtain_mtime(
            robot_yaml_file_info, conda_config_file_info
        )


class _CacheInfo(object):
    """
    As a new instance of the RobocorpResolveInterpreter is created for each call,
    we need to store cached info outside it.
    """

    _cached_file_info: Dict[Path, _CachedFileInfo] = {}
    _cached_interpreter_info: Dict[Path, _CachedInterpreterInfo] = {}
    _cache_hit_files = 0  # Just for testing
    _cache_hit_interpreter = 0  # Just for testing

    @classmethod
    def get_file_info(cls, file_path: Path) -> _CachedFileInfo:
        file_info = cls._cached_file_info.get(file_path)
        if file_info is not None and file_info.is_cache_valid():
            cls._cache_hit_files += 1
            return file_info

        # If it got here, it's not cached or the cache doesn't match.
        file_info = _CachedFileInfo(file_path)
        cls._cached_file_info[file_path] = file_info
        return file_info

    @classmethod
    def get_interpreter_info(
        cls,
        robot_yaml_file_info: _CachedFileInfo,
        conda_config_file_info: Optional[_CachedFileInfo],
        pm: PluginManager,
    ) -> IInterpreterInfo:
        interpreter_info = cls._cached_interpreter_info.get(
            robot_yaml_file_info.file_path
        )
        if interpreter_info is not None and interpreter_info.is_cache_valid(
            robot_yaml_file_info, conda_config_file_info
        ):
            _CacheInfo._cache_hit_interpreter += 1
            return interpreter_info.info

        from robocorp_ls_core.progress_report import progress_context
        from robotframework_ls.ep_providers import EPEndPointProvider

        endpoint = pm[EPEndPointProvider].endpoint

        with progress_context(endpoint, "Obtain env for robot.yaml", dir_cache=None):
            # If it got here, it's not cached or the cache doesn't match.
            # This may take a while...
            interpreter_info = cls._cached_interpreter_info[
                robot_yaml_file_info.file_path
            ] = _CachedInterpreterInfo(robot_yaml_file_info, conda_config_file_info, pm)

            return interpreter_info.info


class RobocorpResolveInterpreter(object):
    """
    Resolves the interpreter based on the robot.yaml found.
    """

    def __init__(self, weak_pm: "weakref.ReferenceType[PluginManager]"):
        self._pm = weak_pm

    @implements(EPResolveInterpreter.get_interpreter_info_for_doc_uri)
    def get_interpreter_info_for_doc_uri(self, doc_uri) -> Optional[IInterpreterInfo]:
        try:
            pm = self._pm()
            if pm is None:
                log.critical("Did not expect PluginManager to be None at this point.")
                return None

            from robotframework_ls.ep_providers import (
                EPConfigurationProvider,
                EPEndPointProvider,
            )

            # Check that our requirements are there.
            pm[EPConfigurationProvider]
            pm[EPEndPointProvider]

            fs_path = Path(uris.to_fs_path(doc_uri))
            for path in fs_path.parents:
                robot_yaml: Path = path / "robot.yaml"
                if robot_yaml.exists():
                    break
            else:
                # i.e.: Could not find any robot.yaml in the structure.
                log.debug("Could not find robot.yaml for: %s", fs_path)
                return None

            # Ok, we have the robot_yaml, so, we should be able to run RCC with it.
            robot_yaml_file_info = _CacheInfo.get_file_info(robot_yaml)
            yaml_contents = robot_yaml_file_info.yaml_contents
            if not isinstance(yaml_contents, dict):
                raise AssertionError(f"Expected dict as root in: {robot_yaml}")

            conda_config = yaml_contents.get("condaConfigFile")
            conda_config_file_info = None

            if conda_config:
                parent: Path = robot_yaml.parent
                conda_config_path = parent.joinpath(conda_config)
                if conda_config_path.exists():
                    conda_config_file_info = _CacheInfo.get_file_info(conda_config_path)

            return _CacheInfo.get_interpreter_info(
                robot_yaml_file_info, conda_config_file_info, pm
            )

        except:
            log.exception(f"Error getting interpreter info for: {doc_uri}")
        return None

    def __typecheckself__(self) -> None:
        from robocorp_ls_core.protocols import check_implements

        _: EPResolveInterpreter = check_implements(self)


def register_plugins(pm: PluginManager):
    pm.register(
        EPResolveInterpreter,
        RobocorpResolveInterpreter,
        kwargs={"weak_pm": weakref.ref(pm)},
    )
