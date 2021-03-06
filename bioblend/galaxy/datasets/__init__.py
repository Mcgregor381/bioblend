"""
Contains possible interactions with the Galaxy Datasets
"""
import logging
import os
import shlex
import time
import warnings
from typing import (
    Any,
    Dict,
    Optional,
)
from urllib.parse import urljoin

import bioblend
from bioblend import TimeoutException
from bioblend.galaxy.client import Client

log = logging.getLogger(__name__)

TERMINAL_STATES = {'ok', 'empty', 'error', 'discarded', 'failed_metadata'}
# Non-terminal states are: 'new', 'upload', 'queued', 'running', 'paused', 'setting_metadata'


class DatasetClient(Client):
    module = 'datasets'

    def __init__(self, galaxy_instance):
        super().__init__(galaxy_instance)

    def show_dataset(self, dataset_id, deleted=False, hda_ldda='hda'):
        """
        Get details about a given dataset. This can be a history or a library dataset.

        :type dataset_id: str
        :param dataset_id: Encoded dataset ID

        :type deleted: bool
        :param deleted: Whether to return results for a deleted dataset

        :type hda_ldda: str
        :param hda_ldda: Whether to show a history dataset ('hda' - the default) or library
                         dataset ('ldda').

        :rtype: dict
        :return: Information about the HDA or LDDA
        """
        params = dict(
            hda_ldda=hda_ldda,
        )
        return self._get(id=dataset_id, deleted=deleted, params=params)

    def download_dataset(self, dataset_id, file_path=None, use_default_filename=True,
                         require_ok_state=True, maxwait=12000):
        """
        Download a dataset to file or in memory. If the dataset state is not
        'ok', a ``DatasetStateException`` will be thrown, unless ``require_ok_state=False``.

        :type dataset_id: str
        :param dataset_id: Encoded dataset ID

        :type file_path: str
        :param file_path: If this argument is provided, the dataset will be streamed to disk
                          at that path (should be a directory if ``use_default_filename=True``).
                          If the file_path argument is not provided, the dataset content is loaded into memory
                          and returned by the method (Memory consumption may be heavy as the entire file
                          will be in memory).

        :type use_default_filename: bool
        :param use_default_filename: If ``True``, the exported
                                 file will be saved as ``file_path/%s``,
                                 where ``%s`` is the dataset name.
                                 If ``False``, ``file_path`` is assumed to
                                 contain the full file path including the filename.

        :type require_ok_state: bool
        :param require_ok_state: If ``False``, datasets will be downloaded even if not in an 'ok' state,
                                 issuing a ``DatasetStateWarning`` rather than raising a ``DatasetStateException``.

        :type maxwait: float
        :param maxwait: Total time (in seconds) to wait for the dataset state to
          become terminal. If the dataset state is not terminal within this
          time, a ``DatasetTimeoutException`` will be thrown.

        :rtype: dict
        :return: If a ``file_path`` argument is not provided, returns a dict containing the file content.
                 Otherwise returns nothing.
        """
        dataset = self.wait_for_dataset(dataset_id, maxwait=maxwait, check=False)
        if not dataset['state'] == 'ok':
            message = "Dataset state is not 'ok'. Dataset id: {}, current state: {}".format(dataset_id, dataset['state'])
            if require_ok_state:
                raise DatasetStateException(message)
            else:
                warnings.warn(message, DatasetStateWarning)

        file_ext = dataset.get('file_ext')
        # Resort to 'data' when Galaxy returns an empty or temporary extension
        if not file_ext or file_ext == 'auto' or file_ext == '_sniff_':
            file_ext = 'data'
        # The preferred download URL is
        # '/api/histories/<history_id>/contents/<dataset_id>/display?to_ext=<dataset_ext>'
        # since the old URL:
        # '/dataset/<dataset_id>/display/to_ext=<dataset_ext>'
        # does not work when using REMOTE_USER with access disabled to
        # everything but /api without auth
        download_url = dataset['download_url'] + '?to_ext=' + file_ext
        url = urljoin(self.gi.base_url, download_url)

        stream_content = file_path is not None
        r = self.gi.make_get_request(url, stream=stream_content)
        r.raise_for_status()

        if file_path is None:
            if 'content-length' in r.headers and len(r.content) != int(r.headers['content-length']):
                log.warning("Transferred content size does not match content-length header (%s != %s)", len(r.content), r.headers['content-length'])
            return r.content
        else:
            if use_default_filename:
                # Build a useable filename
                filename = dataset['name'] + '.' + file_ext
                # Now try to get a better filename from the response headers
                # We expect tokens 'filename' '=' to be followed by the quoted filename
                if 'content-disposition' in r.headers:
                    tokens = list(shlex.shlex(r.headers['content-disposition'], posix=True))
                    try:
                        header_filepath = tokens[tokens.index('filename') + 2]
                        filename = os.path.basename(header_filepath)
                    except (ValueError, IndexError):
                        pass
                file_local_path = os.path.join(file_path, filename)
            else:
                file_local_path = file_path

            with open(file_local_path, 'wb') as fp:
                for chunk in r.iter_content(chunk_size=bioblend.CHUNK_SIZE):
                    if chunk:
                        fp.write(chunk)

            # Return location file was saved to
            return file_local_path

    def get_datasets(self, limit=500, offset=0, history_id=None):
        """
        Get the latest datasets, or select another subset by specifying optional
        arguments for filtering (e.g. a history id).

        Since the number of datasets may be very large, ``limit`` and ``offset``
        parameters should always be used to specify the desired range.

        :type limit: int
        :param limit: Maximum number of datasets to return.

        :type offset: int
        :param offset: Return datasets starting from this specified position.
          For example, if ``limit`` is set to 100 and ``offset`` to 200,
          datasets 200-299 will be returned.

        :type history_id: str
        :param history_id: Encoded history ID to filter on.

        :rtype: list
        :return: Return a list of dataset dicts.
        """
        params = {
            'limit': limit,
            'offset': offset,
        }
        if history_id:
            params['history_id'] = history_id
        return self._get(params=params)

    def publish_dataset(self, dataset_id: str, published: bool = False):
        """
        Make a dataset publicly available or private. For more fine-grained control (assigning different
        permissions to specific roles), use the ``update_permissions()`` method.

        :type dataset_id: str
        :param dataset_id: dataset ID

        :type published: bool
        :param published: Whether to make the dataset published (``True``) or private (``False``).

        :rtype: dict
        :return: Current roles for all available permission types.
        .. note::
          This method can only be used with Galaxy ``release_19.05`` or later.
        """
        payload: Dict[str, Any] = {
            'action': 'remove_restrictions' if published else 'make_private'
        }
        url = self._make_url(dataset_id) + '/permissions'
        self.gi.datasets._put(url=url, payload=payload)

    def update_permissions(self, dataset_id: str, access_ids: Optional[list] = None,
                           manage_ids: Optional[list] = None, modify_ids: Optional[list] = None):
        """
        Set access, manage or modify permissions for a dataset to a list of roles.

        :type dataset_id: str
        :param dataset_id: dataset ID

        :type access_ids: list
        :param access_ids: role IDs which should have access permissions for the dataset.

        :type manage_ids: list
        :param manage_ids: role IDs which should have manage permissions for the dataset.

        :type modify_ids: list
        :param modify_ids: role IDs which should have modify permissions for the dataset.

        :rtype: dict
        :return: Current roles for all available permission types.
        .. note::
          This method can only be used with Galaxy ``release_19.05`` or later.
        """
        payload: Dict[str, Any] = {
            'action': 'set_permissions'
        }
        if access_ids:
            payload['access'] = access_ids
        if manage_ids:
            payload['manage'] = manage_ids
        if modify_ids:
            payload['modify'] = modify_ids
        url = self._make_url(dataset_id) + '/permissions'
        self.gi.datasets._put(url=url, payload=payload)

    def wait_for_dataset(self, dataset_id, maxwait=12000, interval=3, check=True):
        """
        Wait until a dataset is in a terminal state.

        :type dataset_id: str
        :param dataset_id: dataset ID

        :type maxwait: float
        :param maxwait: Total time (in seconds) to wait for the dataset state to
          become terminal. If the dataset state is not terminal within this
          time, a ``DatasetTimeoutException`` will be raised.

        :type interval: float
        :param interval: Time (in seconds) to wait between 2 consecutive checks.

        :type check: bool
        :param check: Whether to check if the dataset terminal state is 'ok'.

        :rtype: dict
        :return: Details of the given dataset.
        """
        assert maxwait >= 0
        assert interval > 0

        time_left = maxwait
        while True:
            dataset = self.show_dataset(dataset_id)
            state = dataset['state']
            if state in TERMINAL_STATES:
                if check and state != 'ok':
                    raise Exception(f"Dataset {dataset_id} is in terminal state {state}")
                return dataset
            if time_left > 0:
                log.info(f"Dataset {dataset_id} is in non-terminal state {state}. Will wait {time_left} more s")
                time.sleep(min(time_left, interval))
                time_left -= interval
            else:
                raise DatasetTimeoutException(f"Dataset {dataset_id} is still in non-terminal state {state} after {maxwait} s")


class DatasetStateException(Exception):
    pass


class DatasetStateWarning(Warning):
    pass


class DatasetTimeoutException(TimeoutException):
    pass
