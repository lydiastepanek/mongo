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
from buildscripts.selected_tests import _find_selected_test_files, _find_selected_tasks, filter_excluded_tasks
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

    origin_build_variants = evg_conf.get_variant(
        "selected-tests").expansions["selected_tests_buildvariants"].split(" ")

    final_results = defaultdict(dict)

    version_ids = [
         # "mongodb_mongo_master_82cf48411f7c6faee2d3dabcce8bb168542dacc2",
         # "mongodb_mongo_master_2ab8c98d285b3cf9481dc34fe77e1a019615f0ad",
         # "mongodb_mongo_master_63253ac8554b2a867de988fed5077ecfbc4522e0",
         # "mongodb_mongo_master_ad642bcf1a2d59600e891c4666a80be4d5f6b4bf",
         # "mongodb_mongo_master_428ac6507118e58b6709e3dbae9fb4657e377637",
         # "mongodb_mongo_master_9fb1edd400526809c917e99ac4cfb6c9473baf72",

        # "mongodb_mongo_master_5e607a45d34a4f977341591eec107a7a8a361626",
        # "mongodb_mongo_master_6aab3ab5b6c20dd46cb659e8eaefb597f2b53263",
        # "mongodb_mongo_master_859b127ed3f86a180010be87cb1b9ccf81db9845",
        # "mongodb_mongo_master_8cdcfd2ab0d28dca863557a02cafc86ae80f960e",
        # "mongodb_mongo_master_a6d5ee2ecb12a8a033f0af31304d132cf69266e5",
        # "mongodb_mongo_master_a929cf1b5d09783bafa93320060aff8a91a76c6e",
        # "mongodb_mongo_master_cde28e2ab957bd4a27ef240dfbfeea3cc8a70b74",

        "mongodb_mongo_master_c8007d0d9574088031d34e70f36f2fcbd17fe253"
    ]
    for version_id in version_ids:
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

            if failed_tasks[build_variant]:
                correctly_captured_tasks = failed_tasks[build_variant].intersection(
                    tasks_that_would_have_run[build_variant])
                percentage_captured_tasks = len(correctly_captured_tasks) / len(
                    failed_tasks[build_variant])
                final_results[version.version_id][build_variant] = percentage_captured_tasks

        LOGGER.info("Failed tasks:", failed_tasks=failed_tasks)
        LOGGER.info("Tasks that would have run:",
                    tasks_that_would_have_run=tasks_that_would_have_run)

    # failed tasks is not printing contents of set()
    LOGGER.info("Final results:", final_results=final_results)


if __name__ == "__main__":
    main()  # pylint: disable=no-value-for-parameter
