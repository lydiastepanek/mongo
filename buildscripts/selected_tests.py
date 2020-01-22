#!/usr/bin/env python3
"""Command line utility for determining what jstests should run for the given changed files."""

import logging
import os
import pdb
import sys
from typing import Dict, List, Optional, Set, Tuple

import click
import requests
import structlog
from evergreen.api import EvergreenApi, RetryingEvergreenApi
from git import Repo
from shrub.config import Configuration
from structlog.stdlib import LoggerFactory

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import buildscripts.resmokelib.parser
import buildscripts.util.read_config as read_config
from buildscripts.burn_in_tests import create_task_list_for_tests
from buildscripts.ciconfig.evergreen import (EvergreenProjectConfig, ResmokeArgs,
                                             parse_evergreen_file)
from buildscripts.evergreen_generate_resmoke_tasks import (
    CONFIG_FORMAT_FN, DEFAULT_CONFIG_VALUES, REQUIRED_CONFIG_KEYS, GenerateSubSuites,
    SelectedTestsConfigOptions, write_file_dict)
from buildscripts.patch_builds.change_data import find_changed_files

structlog.configure(logger_factory=LoggerFactory())
LOGGER = structlog.getLogger(__name__)

EVERGREEN_FILE = "etc/evergreen.yml"
EVG_CONFIG_FILE = ".evergreen.yml"
EXTERNAL_LOGGERS = {
    "evergreen",
    "git",
    "urllib3",
}
EVG_CONFIG_FILE = ".evergreen.yml"
SELECTED_TESTS_CONFIG_DIR = "generated_resmoke_config"


def _configure_logging(verbose: bool):
    """
    Configure logging for the application.

    :param verbose: If True set log level to DEBUG.
    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        format="[%(asctime)s - %(name)s - %(levelname)s] %(message)s",
        level=level,
        stream=sys.stdout,
    )
    for log_name in EXTERNAL_LOGGERS:
        logging.getLogger(log_name).setLevel(logging.WARNING)


def _get_evg_api(evg_api_config: str, local_mode: bool) -> Optional[EvergreenApi]:
    """
    Get an instance of the Evergreen Api.

    :param evg_api_config: Config file with evg auth information.
    :param local_mode: If true, do not connect to Evergreen API.
    :return: Evergreen Api instance.
    """
    if not local_mode:
        return RetryingEvergreenApi.get_api(config_file=evg_api_config)
    return RetryingEvergreenApi.get_api(use_config_file=True)


def _check_file_exists_in_repo(repo, file_path: str) -> bool:
    '''
    Check if file exists in a repo.

    :param repo: The git python repo object.
    :param file_path: The relative path to the file from the repository root.
    return: True if file is found in the repo at the specified path, false otherwise.
    '''
    pathdir = os.path.dirname(file_path)
    #Build up reference to desired repo path
    rsub = repo.head.commit.tree

    for path_element in pathdir.split(os.path.sep):
        # If dir on file path is not in repo, neither is file.
        try:
            rsub = rsub[path_element]
        except KeyError:
            return False

    return (file_path in rsub)


def _filter_deleted_files(repo: Repo, related_test_files: Set[str]) -> Set[str]:
    '''
    Filter out files from a Set if they do not exist in the given repo.

    :param repo: The git python repo object.
    :param related_test_files: Set of files to filer.
    return: Set of files that do exist in repo.
    '''
    return {
        filepath
        for filepath in related_test_files if _check_file_exists_in_repo(repo, filepath)
    }


def _find_related_test_files(selected_tests_auth_user: str, selected_tests_auth_token: str,
                             changed_files: Set[str], repo: Repo) -> Set[str]:
    '''
    For a list of changed files, make a request to the selected-tests service to return all test
    files related to these changed files, then filter the test files.

    :param selected_tests_auth_user: Selected-tests service auth user to authenticate request.
    :param selected_tests_auth_token: Selected-tests service auth token to authenticate request.
    :param changed_files: Set of changed_files.
    :param repo: The git python repo object.
    return: Set of test files returned by selected-tests service that do exist in repo.
    '''
    payload = {'threshold': .1, 'changed_files': ",".join(changed_files)}
    headers = {'Content-type': 'application/json', 'Accept': 'application/json'}
    cookies = dict(auth_user=selected_tests_auth_user, auth_token=selected_tests_auth_token)
    response = requests.get(
        "https://selected-tests.server-tig.prod.corp.mongodb.com/projects/mongodb-mongo-master/test-mappings",
        params=payload, headers=headers, cookies=cookies).json()
    related_test_files = {
        test_file['name']
        for test_mapping in response['test_mappings'] for test_file in test_mapping['test_files']
    }
    return _filter_deleted_files(repo, related_test_files)


def _create_overwrite_values(evg_conf: EvergreenProjectConfig, build_variant: str, task_name: str,
                             burn_in_task_config: dict):
    '''
    Create overwrite values to pass in to initialize a SelectedTestsConfigOptions object.

    :param evg_conf: Evergreen configuration.
    :param build_variant: Build variant to collect task info from.
    :param task_name: Name of task to get info for.
    :param burn_in_task_config: The value for a given task_name in the tests_by_task dict.
    return: Overwrite values.
    '''
    evg_build_variant = evg_conf.get_variant(build_variant)
    task = evg_build_variant.get_task(task_name)
    if task.is_generate_resmoke_task:
        task_vars = task.generate_resmoke_tasks_command["vars"]
    else:
        task_vars = task.run_tests_command["vars"]
        task_vars.update({'fallback_num_sub_suites': '1'})

    tests_to_run = " ".join(burn_in_task_config['tests'])
    task_vars['resmoke_args'] = "{} {}".format(task_vars['resmoke_args'], tests_to_run)

    overwrite_values = {"task_name": task_name, **task_vars}
    suite_name = ResmokeArgs.get_arg(task_vars['resmoke_args'], "suites")
    if suite_name:
        overwrite_values.update({"suite": suite_name})
    return overwrite_values


def _generate_shrub_config(evg_api, evg_conf, expansion_file, tests_by_task, build_variant):
    '''
    Generate a dict containing file names and contents for the json file containing generated 
    tasks config and the yml files containing suite configs.

    :param evg_api: Evergreen API object.
    :param evg_conf: Evergreen configuration.
    :param expansion_file: Configuration file.
    :param tests_by_task: Dictionary of tests and tasks to run.
    :param build_variant: Build variant to collect task info from.
    return: Dict of files and file contents for generated tasks.
    '''
    shrub_config = Configuration()
    config_file_dict = {}
    shrub_config_json = {}
    for task_name, burn_in_task_config in tests_by_task.items():
        overwrite_values = _create_overwrite_values(evg_conf, build_variant, task_name,
                                                    burn_in_task_config)
        config_options = SelectedTestsConfigOptions.from_file(expansion_file, REQUIRED_CONFIG_KEYS,
                                                              DEFAULT_CONFIG_VALUES,
                                                              CONFIG_FORMAT_FN, overwrite_values)
        suite_file_dict, shrub_config_json = GenerateSubSuites(
            evg_api, config_options).generate_config_dict(shrub_config)
        config_file_dict.update(suite_file_dict)
    config_file_dict["selected_tests_config.json"] = shrub_config_json
    return config_file_dict


@click.command()
@click.option("--verbose", "verbose", default=False, is_flag=True, help="Enable extra logging.")
@click.option("--expansion-file", "expansion_file", type=str, required=True,
              help="Location of expansions file generated by evergreen.")
@click.option("--evg-api-config", "evg_api_config", default=EVG_CONFIG_FILE, metavar="FILE",
              help="Configuration file with connection info for Evergreen API.")
@click.option("--local", "local_mode", default=False, is_flag=True,
              help="Local mode. Do not call out to evergreen api.")
@click.option("--build-variant", "build_variant", default=None, metavar='BUILD_VARIANT',
              help="Tasks to run will be selected from this build variant.")
@click.option("--generate-tasks-file", "generate_tasks_file", default=None, metavar='FILE',
              help="Run in 'generate.tasks' mode. Store task config to given file.")
@click.option("--selected-tests-auth-user", "selected_tests_auth_user", required=True,
              help="Auth user for selected-tests service.")
@click.option("--selected-tests-auth-token", "selected_tests_auth_token", required=True,
              help="Auth token for selected-tests service.")
def main(verbose, expansion_file, evg_api_config, local_mode, build_variant, generate_tasks_file,
         selected_tests_auth_user, selected_tests_auth_token):
    """Execute Main program."""
    _configure_logging(verbose)

    evg_api = _get_evg_api(evg_api_config, local_mode)
    evg_conf = parse_evergreen_file(EVERGREEN_FILE)

    repo = Repo(".")
    changed_files = find_changed_files(repo)
    buildscripts.resmokelib.parser.set_options()
    LOGGER.debug("Found changed files", files=changed_files)
    related_test_files = _find_related_test_files(selected_tests_auth_user,
                                                  selected_tests_auth_token, changed_files, repo)
    LOGGER.debug("related test files found", related_test_files=related_test_files)
    if related_test_files:
        tests_by_task = create_task_list_for_tests(related_test_files, build_variant, evg_conf)
        LOGGER.debug("tests and tasks found", tests_by_task=tests_by_task)
        config_file_dict = _generate_shrub_config(evg_api, evg_conf, expansion_file, tests_by_task,
                                                  build_variant)

        write_file_dict(SELECTED_TESTS_CONFIG_DIR, config_file_dict)
    else:
        LOGGER.debug("No valid test files related to changed files")


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
