#!/usr/bin/env python3
"""Selected Tests service."""

from typing import Set

import requests
import yaml

# pylint: disable=wrong-import-position
from buildscripts.burn_in_tests import is_file_a_test_file


class SelectedTestsService(object):
    """Selected-tests client object."""

    def __init__(self, url: str, auth_user: str, auth_token: str):
        """
        Create selected-tests client object.

        :param url: Selected-tests service url.
        :param auth_user: Selected-tests service auth user to authenticate request.
        :param auth_token: Selected-tests service auth token to authenticate request.
        """
        self.url = url
        self.auth_user = auth_user
        self.auth_token = auth_token
        self.headers = {"Content-type": "application/json", "Accept": "application/json"}
        self.cookies = {"auth_user": auth_user, "auth_token": auth_token}

    @classmethod
    def from_file(cls, filename: str):
        """
        Read config from given filename.

        :param filename: Filename to read config.
        :return: Config read from file.
        """
        with open(filename, 'r') as fstream:
            config = yaml.safe_load(fstream)
            if config:
                return cls(config["url"], config["auth_user"], config["auth_token"])

        return None

    def get_test_mappings(self, threshold: float, changed_files: Set[str]):
        """
        Request related test files from selected-tests service and filter them.

        :param threshold: Threshold for test file correlation.
        :param changed_files: Set of changed_files.
        return: Set of related test files returned by selected-tests service.
        """
        payload = {"threshold": threshold, "changed_files": ",".join(changed_files)}
        response = requests.get(
            self.url + "/projects/mongodb-mongo-master/test-mappings",
            params=payload,
            headers=self.headers,
            cookies=self.cookies,
        ).json()
        return response["test_mappings"]
