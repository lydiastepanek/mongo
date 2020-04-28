#!/usr/bin/env python3

import logging
import os
import sys
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict
import pytz
import pdb

import click
import structlog
from structlog.stdlib import LoggerFactory
from evergreen.api import EvergreenApi, RetryingEvergreenApi
from git import Repo
from datetime import datetime

# Get relative imports to work when the package is not installed on the PYTHONPATH.
if __name__ == "__main__" and __package__ is None:
    sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pylint: disable=wrong-import-position
import buildscripts.resmokelib.parser
import buildscripts.util.read_config as read_config
from buildscripts.burn_in_tests import DEFAULT_REPO_LOCATIONS, create_task_list_for_tests
from buildscripts.selected_tests import _find_selected_test_files, _find_selected_tasks, \
    filter_excluded_tasks, _remove_repo_path_prefix
from buildscripts.ciconfig.evergreen import (
    parse_evergreen_file, )
from buildscripts.patch_builds.change_data import find_changed_files
from buildscripts.patch_builds.selected_tests_service import SelectedTestsService

structlog.configure(logger_factory=LoggerFactory())
LOGGER = structlog.getLogger(__name__)

EVERGREEN_FILE = "etc/evergreen.yml"
EVG_CONFIG_FILE = ".evergreen.yml"
DEFAULT_PROJECT = "mongodb-mongo-master"
EXTERNAL_LOGGERS = {
    "evergreen",
    "git",
    "urllib3",
}


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


@click.command()
@click.option("--verbose", "verbose", default=False, is_flag=True, help="Enable extra logging.")
@click.option("--project", "project", default=DEFAULT_PROJECT, metavar='PROJECT',
              help="The evergreen project the tasks will execute on.")
@click.option(
    "--evg-api-config",
    "evg_api_config",
    default=EVG_CONFIG_FILE,
    metavar="FILE",
    help="Configuration file with connection info for Evergreen API.",
)
@click.option(
    "--selected-tests-config",
    "selected_tests_config",
    required=True,
    metavar="FILE",
    help="Configuration file with connection info for selected tests service.",
)
def main(
        verbose: bool,
        project: str,
        evg_api_config: str,
        selected_tests_config: str,
):
    """
    Select tasks to be run based on changed files in a patch build.

    :param verbose: Log extra debug information.
    :param project: Project to run tests on.
    :param evg_api_config: Location of configuration file to connect to evergreen.
    :param selected_tests_config: Location of config file to connect to elected-tests service.
    """
    _configure_logging(verbose)

    evg_api = RetryingEvergreenApi.get_api(config_file=evg_api_config)
    evg_conf = parse_evergreen_file(EVERGREEN_FILE)
    selected_tests_service = SelectedTestsService.from_file(selected_tests_config)

    mongo_repo = Repo(".")
    enterprise_repo = Repo("./src/mongo/db/modules/enterprise")

    buildscripts.resmokelib.parser.set_options()

    final_results = defaultdict(dict)

    version_ids = [
        "mongodb_mongo_master_250cae2fdcd06600435d9f80de79f610e2c84df8",
        #  "mongodb_mongo_master_cad10292bbd4f8e237c5ba85ec9265c21eddec38",
        "mongodb_mongo_master_e14dbefec5fb18a7e9fc8739d3ef529bb1338ab4",
        "mongodb_mongo_master_25e8528e420bd128cd0f944aba37afce3907276e",
        "mongodb_mongo_master_b202ee3df460192bddf4193076c346928457a150",
        "mongodb_mongo_master_546e411b72cf6f75d24b304ce9219d1f3d3a4e4f",
        "mongodb_mongo_master_aa2ccf6e1992b41ac1b286291e6217d91157f573",
        "mongodb_mongo_master_9faba94cb86061f5acba467cdd5f88338e712c1f",
        "mongodb_mongo_master_9c15f7ff0f43d0813aec101135800d285a0cb54b",
    ]
    for version_id in version_ids:
        origin_build_variants = ["linux-64-debug", "enterprise-rhel-62-64-bit"]
        #  origin_build_variants = evg_conf.get_variant(
            #  "selected-tests").expansions["selected_tests_buildvariants"].split(" ")
        version = evg_api.version_by_id(version_id)
        LOGGER.info("Analyzing version", version=version.version_id,
                    create_time=version.create_time)

        tasks_that_would_have_run = defaultdict(set)
        failed_tasks = {}

        changed_files = set()
        mongo_commit = mongo_repo.commit(version.revision)
        enterprise_commit = enterprise_repo.commit(
            version.get_manifest().modules["enterprise"].revision)

        for commit, repo in {mongo_commit: mongo_repo, enterprise_commit: enterprise_repo}.items():
            parent = commit.parents[0]
            diff = commit.diff(parent)
            repo_changed_files = find_changed_files(diff, repo)
            changed_files.update(repo_changed_files)

        changed_files = {_remove_repo_path_prefix(file_path) for file_path in changed_files}

        LOGGER.info("Found changed files", files=changed_files)

        for build_variant in origin_build_variants:

            evg_build = version.build_by_variant(build_variant)
            build_variant_config = evg_conf.get_variant(build_variant)

            failed_tasks_for_build = {
                task.display_name
                for task in evg_build.get_tasks() if task.status.lower() == "failed"
            }
            failed_tasks[build_variant] = filter_excluded_tasks(build_variant_config,
                                                                failed_tasks_for_build)

            related_test_files = _find_selected_test_files(selected_tests_service, changed_files)
            LOGGER.debug("related test files found", related_test_files=related_test_files)
            if related_test_files:
                tests_by_task = create_task_list_for_tests(related_test_files, build_variant,
                                                           evg_conf)
                LOGGER.debug("tests and tasks found", tests_by_task=tests_by_task)
                tasks_that_would_have_run[build_variant].update(tests_by_task.keys())

            related_tasks = _find_selected_tasks(selected_tests_service, changed_files,
                                                 build_variant_config)
            LOGGER.debug("related tasks found", related_tasks=related_tasks)
            if related_tasks:
                tasks_that_would_have_run[build_variant].update(related_tasks)

            tasks_that_would_have_run[build_variant] = filter_excluded_tasks(
                build_variant_config, tasks_that_would_have_run[build_variant])

            if failed_tasks[build_variant]:
                correctly_captured_tasks = failed_tasks[build_variant].intersection(
                    tasks_that_would_have_run[build_variant])
                percentage_captured_tasks = len(correctly_captured_tasks) / len(
                    failed_tasks[build_variant])
                final_results[version.version_id][build_variant] = {
                    "tasks_selected_to_run": len(tasks_that_would_have_run[build_variant]),
                    "percentage_captured_tasks": percentage_captured_tasks
                }

        LOGGER.info("Failed tasks:", failed_tasks=failed_tasks)
        LOGGER.info("Tasks that would have run:",
                    tasks_that_would_have_run=tasks_that_would_have_run)

    # failed tasks is not printing contents of set()
    LOGGER.info("Final results:", final_results=final_results)


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
