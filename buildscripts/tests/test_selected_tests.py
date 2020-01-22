"""Unit tests for the selected_tests script."""
import os

import unittest
import git
from tempfile import TemporaryDirectory
import pdb
from mock import Mock, patch, MagicMock

import buildscripts.ciconfig.evergreen as _evergreen
from buildscripts import selected_tests as under_test

NS = "buildscripts.selected_tests"


def ns(relative_name):  # pylint: disable=invalid-name
    """Return a full name from a name relative to the test module"s name space."""
    return NS + "." + relative_name


def repo_with_one_files_1_and_2(temp_directory):
    repo = git.Repo.init(temp_directory)
    myTemp = os.path.join(temp_directory, 'jstests')
    os.makedirs(myTemp)
    file_1 = os.path.join(myTemp, "file-1.js")
    file_2 = os.path.join(myTemp, "file-2.js")
    open(file_1, "wb").close()
    open(file_2, "wb").close()
    repo.index.add([file_1, file_2])
    repo.index.commit("add files")
    return repo


def tests_by_task_stub():
    return {
        "jsCore_auth": {
            'display_task_name': 'jsCore_auth', 'resmoke_args': '--suites=core_auth', 'tests': [
                'jstests/core/currentop_waiting_for_latch.js', 'jstests/core/latch_analyzer.js'
            ], 'use_multiversion': None, 'distro': 'rhel62-small'
        }, "auth_gen": {
            'display_task_name': 'auth', 'resmoke_args': '--suites=auth --storageEngine=wiredTiger',
            'tests': ['jstests/auth/auth3.js'], 'use_multiversion': None, 'distro': 'rhel62-small'
        }
    }


def generate_resmoke_task_stub(task_name):
    task_dict = {
        "name":
            task_name,
        "commands": [{
            "func": "generate resmoke tasks",
            "vars": {'fallback_num_sub_suites': '4', 'resmoke_args': '--storageEngine=wiredTiger'}
        }]
    }
    return _evergreen.Task(task_dict)


def non_generate_resmoke_task_stub(task_name):
    task_dict = {
        "name": task_name,
        "commands": [{"func": "run tests", "vars": {"resmoke_args": "--suites=core_auth"}}]
    }
    return _evergreen.Task(task_dict)


class TestCheckFileExistsInRepo(unittest.TestCase):
    def test_file_is_in_repo(self):
        with TemporaryDirectory() as tmpdir:
            repo = repo_with_one_files_1_and_2(tmpdir)
            # file_name argument must be a relative file path, not absolute file path
            file_path = "jstests/file-1.js"

            self.assertTrue(under_test._check_file_exists_in_repo(repo, file_path))

    def test_file_is_not_in_repo(self):
        with TemporaryDirectory() as tmpdir:
            repo = repo_with_one_files_1_and_2(tmpdir)
            # file_name argument must be a relative file path, not absolute file path
            file_path = "jstests/file-3.js"

            self.assertFalse(under_test._check_file_exists_in_repo(repo, file_path))


class TestFilterRelatedTestFiles(unittest.TestCase):
    def test_filters_correct_files(self):
        with TemporaryDirectory() as tmpdir:
            repo = repo_with_one_files_1_and_2(tmpdir)
            related_test_files = {"jstests/file-1.js", "jstests/file-3.js"}
            filtered_test_files = under_test._filter_deleted_files(repo, related_test_files)

            self.assertEqual(filtered_test_files, {"jstests/file-1.js"})


class TestFindTestFilesRelatedToChangedFiles(unittest.TestCase):
    @patch(ns("requests"))
    def test_files_returned(self, requests_mock):
        changed_files = {"src/file1.cpp", "src/file2.js"}
        response_object = {
            "test_mappings": [
                {"source_file": "src/file1.cpp", "test_files": [{"name": "jstests/file-1.js"}]},
                {"source_file": "src/file2.cpp", "test_files": [{"name": "jstests/file-3.js"}]},
            ]
        }
        requests_mock.get.return_value.json.return_value = response_object

        with TemporaryDirectory() as tmpdir:
            repo = repo_with_one_files_1_and_2(tmpdir)
            related_test_files = under_test._find_related_test_files("auth_user", "auth_token",
                                                                     changed_files, repo)

            requests_mock.get.assert_called_with(
                'https://selected-tests.server-tig.prod.corp.mongodb.com/projects/mongodb-mongo-master/test-mappings',
                params={'changed_files': ",".join(changed_files)}, headers={
                    'Content-type': 'application/json', 'Accept': 'application/json'
                }, cookies={"auth_user": "auth_user", "auth_token": "auth_token"})
            self.assertEqual(related_test_files, {"jstests/file-1.js"})

    @patch(ns("requests"))
    def no_test_files_returned(self, requests_mock):
        changed_files = {"src/file1.cpp", "src/file2.js"}
        response_object = {"test_mappings": []}
        requests_mock.get.return_value.json.return_value = response_object

        with TemporaryDirectory() as tmpdir:
            repo = repo_with_one_files_1_and_2(tmpdir)
            related_test_files = under_test._find_related_test_files("auth_user", "auth_token",
                                                                     changed_files, repo)

            self.assertEqual(related_test_files, [])


class TestGetOverwriteValues(unittest.TestCase):
    def test_task_is_a_generate_resmoke_task(self):
        task_name = "auth_gen"
        task = generate_resmoke_task_stub(task_name)
        burn_in_task_config = tests_by_task_stub()[task_name]
        evg_conf_mock = MagicMock()
        evg_conf_mock.get_variant.return_value.get_task.return_value = task
        overwrite_values = under_test._get_overwrite_values(evg_conf_mock, "variant", task_name,
                                                            burn_in_task_config)
        # {'task_name': 'auth_gen', 's3_bucket_task_name': 'selected_tests',
        # 'fallback_num_sub_suites': '4', 'resmoke_args':
        # '--storageEngine=wiredTiger jstests/auth/auth3.js'}
        self.assertEqual(overwrite_values["s3_bucket_task_name"], "selected_tests")
        self.assertEqual(overwrite_values["task_name"], task_name)
        # suite should be empty
        self.assertIsNone(overwrite_values.get("suite"))
        self.assertEqual(overwrite_values["resmoke_args"],
                         '--storageEngine=wiredTiger jstests/auth/auth3.js')
        self.assertEqual(overwrite_values["fallback_num_sub_suites"], "4")
        self.assertIsNone(overwrite_values.get("display_task_suffix"))

    def test_task_is_not_a_generate_resmoke_task(self):
        task_name = "jsCore_auth"
        task = non_generate_resmoke_task_stub(task_name)
        burn_in_task_config = tests_by_task_stub()[task_name]
        evg_conf_mock = MagicMock()
        evg_conf_mock.get_variant.return_value.get_task.return_value = task
        overwrite_values = under_test._get_overwrite_values(evg_conf_mock, "variant", task_name,
                                                            burn_in_task_config)
        #  {'task_name': 'jsCore_auth', 's3_bucket_task_name': 'selected_tests', 'resmoke_args': '--suites=core_auth jstests/core/currentop_waiting_for_latch.js jstests/core/latch_analyzer.js', 'fallback_num_sub_suites': '1'}
        self.assertEqual(overwrite_values["s3_bucket_task_name"], "selected_tests")
        self.assertEqual(overwrite_values["task_name"], task_name)
        self.assertEqual(overwrite_values["suite"], "core_auth")
        self.assertEqual(
            overwrite_values["resmoke_args"],
            '--suites=core_auth jstests/core/currentop_waiting_for_latch.js jstests/core/latch_analyzer.js'
        )
        self.assertEqual(overwrite_values["fallback_num_sub_suites"], "1")
        self.assertEqual(overwrite_values["display_task_suffix"], "_variant")


class TestGenerateShrubConfig(unittest.TestCase):
    @patch(ns("_get_overwrite_values"))
    @patch(ns("ConfigOptions"))
    @patch(ns("GenerateSubSuites"))
    def test_generate(self, generate_subsuites_mock, config_options_mock,
                      get_overwrite_values_mock):
        evg_api = MagicMock()
        evg_conf = MagicMock()
        expansion_file = MagicMock()
        tests_by_task = tests_by_task_stub()
        yml_suite_file_contents = MagicMock()
        shrub_json_file_contents = MagicMock()
        suite_file_dict_mock = {'auth_0.yml': yml_suite_file_contents}
        generate_subsuites_mock.return_value.generate_config_dict.return_value = (
            suite_file_dict_mock, shrub_json_file_contents)
        config_file_dict = under_test._generate_shrub_config(evg_api, evg_conf, expansion_file,
                                                             tests_by_task, "variant")
        self.assertEqual(
            config_file_dict, {
                'auth_0.yml': yml_suite_file_contents,
                'selected_tests_config.json': shrub_json_file_contents
            })
