# Copyright (c) Microsoft Corporation
#
# All rights reserved.
#
# MIT License
#
# Permission is hereby granted, free of charge, to any person obtaining a
# copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED *AS IS*, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
# FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

# compat imports
from __future__ import (
    absolute_import, division, print_function, unicode_literals
)
from builtins import (  # noqa
    bytes, dict, int, list, object, range, ascii, chr, hex, input,
    next, oct, open, pow, round, super, filter, map, zip)
# stdlib imports
import enum
import logging
import math
try:
    import pathlib2 as pathlib
except ImportError:  # noqa
    import pathlib
try:
    import queue
except ImportError:  # noqa
    import Queue as queue
import threading
# non-stdlib imports
# local imports
import blobxfer.models.crypto
import blobxfer.operations.azure.blob
import blobxfer.operations.azure.file
import blobxfer.operations.crypto
import blobxfer.operations.md5
import blobxfer.operations.progress
import blobxfer.operations.resume
import blobxfer.util

# create logger
logger = logging.getLogger(__name__)


class UploadAction(enum.Enum):
    Skip = 1
    CheckMd5 = 2
    Upload = 3


class Uploader(object):
    """Uploader"""
    def __init__(self, general_options, creds, spec):
        # type: (Uploader, blobxfer.models.options.General,
        #        blobxfer.operations.azure.StorageCredentials,
        #        blobxfer.models.upload.Specification) -> None
        """Ctor for Uploader
        :param Uploader self: this
        :param blobxfer.models.options.General general_options: general opts
        :param blobxfer.operations.azure.StorageCredentials creds: creds
        :param blobxfer.models.uplaod.Specification spec: upload spec
        """
        self._all_local_files_processed = False
        self._crypto_offload = None
        self._md5_meta_lock = threading.Lock()
        self._md5_map = {}
        self._md5_offload = None
        self._upload_lock = threading.Lock()
        self._upload_queue = queue.Queue()
        self._upload_set = set()
        self._upload_start_time = None
        self._upload_threads = []
        self._upload_total = None
        self._upload_sofar = 0
        self._upload_bytes_total = None
        self._upload_bytes_sofar = 0
        self._upload_terminate = False
        self._transfer_lock = threading.Lock()
        self._transfer_queue = queue.Queue()
        self._transfer_set = set()
        self._transfer_threads = []
        self._start_time = None
        self._delete_exclude = set()
        self._ud_map = {}
        self._containers_created = set()
        self._fileshare_dir_lock = threading.Lock()
        self._dirs_created = {}
        self._general_options = general_options
        self._creds = creds
        self._spec = spec
        self._resume = None
        self._exceptions = []

    @property
    def termination_check(self):
        # type: (Uploader) -> bool
        """Check if terminated
        :param Uploader self: this
        :rtype: bool
        :return: if terminated
        """
        with self._upload_lock:
            with self._transfer_lock:
                return (self._upload_terminate or
                        len(self._exceptions) > 0 or
                        (self._all_local_files_processed and
                         len(self._upload_set) == 0 and
                         len(self._transfer_set) == 0))

    @property
    def termination_check_md5(self):
        # type: (Uploader) -> bool
        """Check if terminated from MD5 context
        :param Uploader self: this
        :rtype: bool
        :return: if terminated from MD5 context
        """
        with self._md5_meta_lock:
            with self._upload_lock:
                return (self._upload_terminate or
                        (self._all_local_files_processed and
                         len(self._md5_map) == 0 and
                         len(self._upload_set) == 0))

    def _update_progress_bar(self):
        # type: (Uploader) -> None
        """Update progress bar
        :param Uploader self: this
        """
        blobxfer.operations.progress.update_progress_bar(
            self._general_options,
            'upload',
            self._upload_start_time,
            self._upload_total,
            self._upload_sofar,
            self._upload_bytes_total,
            self._upload_bytes_sofar,
        )

    def _pre_md5_skip_on_check(self, src, rfile):
        # type: (Uploader, blobxfer.models.upload.LocalPath,
        #        blobxfer.models.azure.StorageEntity) -> None
        """Perform pre MD5 skip on check
        :param Uploader self: this
        :param blobxfer.models.upload.LocalPath src: local path
        :param blobxfer.models.azure.StorageEntity rfile: remote file
        """
        # if encryption metadata is present, check for pre-encryption
        # md5 in blobxfer extensions
        md5 = None
        if rfile.encryption_metadata is not None:
            md5 = rfile.encryption_metadata.blobxfer_extensions.\
                pre_encrypted_content_md5
        if md5 is None:
            md5 = rfile.md5
        slpath = str(src.absolute_path)
        with self._md5_meta_lock:
            self._md5_map[slpath] = (src, rfile)
        self._md5_offload.add_localfile_for_md5_check(slpath, md5, rfile.mode)

    def _post_md5_skip_on_check(self, filename, md5_match):
        # type: (Uploader, str, bool) -> None
        """Perform post MD5 skip on check
        :param Uploader self: this
        :param str filename: local filename
        :param bool md5_match: if MD5 matches
        """
        uid = self._create_unique_id(src, rfile)
        with self._md5_meta_lock:
            src, rfile = self._md5_map.pop(filename)
        if md5_match:
            with self._upload_lock:
                self._upload_set.remove(uid)
                self._upload_total -= 1
                self._upload_bytes_total -= src.size
        else:
            self._add_to_upload_queue(src, rfile, uid)

    def _check_for_uploads_from_md5(self):
        # type: (Uploader) -> None
        """Check queue for a file to upload
        :param Uploader self: this
        """
        cv = self._md5_offload.done_cv
        while not self.termination_check_md5:
            result = None
            cv.acquire()
            while True:
                result = self._md5_offload.pop_done_queue()
                if result is None:
                    # use cv timeout due to possible non-wake while running
                    cv.wait(1)
                    # check for terminating conditions
                    if self.termination_check_md5:
                        break
                else:
                    break
            cv.release()
            if result is not None:
                self._post_md5_skip_on_check(result[0], result[1])

    def _check_for_crypto_done(self):
        # type: (Uploader) -> None
        """Check queue for crypto done
        :param Uploader self: this
        """
        cv = self._crypto_offload.done_cv
        while not self.termination_check:
            result = None
            cv.acquire()
            while True:
                result = self._crypto_offload.pop_done_queue()
                if result is None:
                    # use cv timeout due to possible non-wake while running
                    cv.wait(1)
                    # check for terminating conditions
                    if self.termination_check:
                        break
                else:
                    break
            cv.release()
            if result is not None:
                try:
                    with self._upload_lock:
                        dd = self._ud_map[result]
                    dd.perform_chunked_integrity_check()
                except KeyError:
                    # this can happen if all of the last integrity
                    # chunks are processed at once
                    pass

    def _add_to_upload_queue(self, src, rfile, uid):
        # type: (Uploader, blobxfer.models.upload.LocalPath,
        #        blobxfer.models.azure.StorageEntity, str) -> None
        """Add remote file to download queue
        :param Uploader self: this
        :param blobxfer.models.upload.LocalPath src: local path
        :param blobxfer.models.azure.StorageEntity rfile: remote file
        :param str uid: unique id
        """
        # prepare local file for upload
        ud = blobxfer.models.upload.Descriptor(
            src, rfile, uid, self._spec.options, self._resume)
        if ud.entity.is_encrypted:
            with self._upload_lock:
                self._ud_map[uid] = ud
        # add download descriptor to queue
        self._upload_queue.put(ud)
        if self._upload_start_time is None:
            with self._upload_lock:
                if self._upload_start_time is None:
                    self._upload_start_time = blobxfer.util.datetime_now()

    def _initialize_upload_threads(self):
        # type: (Uploader) -> None
        """Initialize upload threads
        :param Uploader self: this
        """
        logger.debug('spawning {} transfer threads'.format(
            self._general_options.concurrency.transfer_threads))
        for _ in range(self._general_options.concurrency.transfer_threads):
            thr = threading.Thread(target=self._worker_thread_upload)
            self._upload_threads.append(thr)
            thr.start()

    def _initialize_transfer_threads(self):
        # type: (Uploader) -> None
        """Initialize transfer threads
        :param Uploader self: this
        """
        logger.debug('spawning {} transfer threads'.format(
            self._general_options.concurrency.transfer_threads))
        for _ in range(self._general_options.concurrency.transfer_threads):
            thr = threading.Thread(target=self._worker_thread_transfer)
            self._transfer_threads.append(thr)
            thr.start()

    def _wait_for_upload_threads(self, terminate):
        # type: (Uploader, bool) -> None
        """Wait for upload threads
        :param Uploader self: this
        :param bool terminate: terminate threads
        """
        if terminate:
            self._upload_terminate = terminate
        for thr in self._upload_threads:
            thr.join()

    def _wait_for_transfer_threads(self, terminate):
        # type: (Uploader, bool) -> None
        """Wait for transfer threads
        :param Uploader self: this
        :param bool terminate: terminate threads
        """
        if terminate:
            self._upload_terminate = terminate
        for thr in self._transfer_threads:
            thr.join()

    def _worker_thread_transfer(self):
        # type: (Uploader) -> None
        """Worker thread transfer
        :param Uploader self: this
        """
        while not self.termination_check:
            try:
                ud, ase, offsets, data = self._transfer_queue.get(
                    block=False, timeout=0.03)
            except queue.Empty:
                continue
            try:
                self._process_transfer(ud, ase, offsets, data)
            except Exception as e:
                with self._upload_lock:
                    self._exceptions.append(e)

    def _process_transfer(self, ud, ase, offsets, data):
        # issue put range
        self._put_data(ud, ase, offsets, data)
        # accounting
        with self._transfer_lock:
            self._transfer_set.remove(
                self._create_unique_transfer_id(ud.local_path, ase, offsets))
            self._upload_bytes_sofar += offsets.num_bytes
        ud.complete_offset_upload()

    def _put_data(self, ud, ase, offsets, data):
        print('UL', offsets)
        if ase.mode == blobxfer.models.azure.StorageModes.Append:
            raise NotImplementedError()
        elif ase.mode == blobxfer.models.azure.StorageModes.Block:
            if offsets.chunk_num == 0:
                # create container if necessary
                blobxfer.operations.azure.blob.create_container(
                    ase, self._containers_created,
                    timeout=self._general_options.timeout_sec)
                # handle one-shot uploads
                if ud.is_one_shot_block_blob:
                    metadata = ud.generate_metadata()
                    if ud.must_compute_md5:
                        digest = blobxfer.util.base64_encode_as_string(
                            ud.md5.digest())
                    else:
                        digest = None
                    blobxfer.operations.azure.blob.block.create_blob(
                        ase, data, digest, metadata,
                        timeout=self._general_options.timeout_sec)
                    return
            # upload block
            blobxfer.operations.azure.blob.block.put_block(
                ase, offsets, data, timeout=self._general_options.timeout_sec)
        elif ase.mode == blobxfer.models.azure.StorageModes.File:
            if offsets.chunk_num == 0:
                # create container if necessary
                blobxfer.operations.azure.file.create_share(
                    ase, self._containers_created,
                    timeout=self._general_options.timeout_sec)
                # create parent directories
                with self._fileshare_dir_lock:
                    blobxfer.operations.azure.file.\
                        create_all_parent_directories(
                            ase, self._dirs_created,
                            timeout=self._general_options.timeout_sec)
                # create remote file
                blobxfer.operations.azure.file.create_file(
                    ase, timeout=self._general_options.timeout_sec)
            # upload range
            if data is not None:
                blobxfer.operations.azure.file.put_file_range(
                    ase, offsets, data,
                    timeout=self._general_options.timeout_sec)
        elif ase.mode == blobxfer.models.azure.StorageModes.Page:
            if offsets.chunk_num == 0:
                # create container if necessary
                blobxfer.operations.azure.blob.create_container(
                    ase, self._containers_created,
                    timeout=self._general_options.timeout_sec)
                # create remote blob
                blobxfer.operations.azure.blob.page.create_blob(
                    ase, timeout=self._general_options.timeout_sec)
            if data is None:
                return
            # align page
            aligned = blobxfer.util.page_align_content_length(
                offsets.num_bytes)
            if aligned != offsets.num_bytes:
                data = data.ljust(aligned, b'\0')
            if blobxfer.operations.md5.check_data_is_empty(data):
                return
            # upload page
            blobxfer.operations.azure.blob.page.put_page(
                ase, offsets.range_start, offsets.range_start + aligned - 1,
                data, timeout=self._general_options.timeout_sec)

    def _worker_thread_upload(self):
        # type: (Uploader) -> None
        """Worker thread upload
        :param Uploader self: this
        """
        import time
        while not self.termination_check:
            try:
                if (len(self._transfer_set) >=
                        self._general_options.concurrency.transfer_threads * 2):
                    time.sleep(0.5)
                    continue
                else:
                    ud = self._upload_queue.get(False, 0.03)
            except queue.Empty:
                continue
            try:
                self._process_upload_descriptor(ud)
            except Exception as e:
                with self._upload_lock:
                    self._exceptions.append(e)

    def _process_upload_descriptor(self, ud):
        # type: (Uploader, blobxfer.models.upload.Descriptor) -> None
        """Process upload descriptor
        :param Uploader self: this
        :param blobxfer.models.upload.Descriptor: upload descriptor
        """
        # update progress bar
        self._update_progress_bar()
        # get download offsets
        offsets, resume_bytes = ud.next_offsets()
        # add resume bytes to counter
        if resume_bytes is not None:
            with self._transfer_lock:
                self._upload_bytes_sofar += resume_bytes
                logger.debug('adding {} sofar {} from {}'.format(
                    resume_bytes, self._upload_bytes_sofar, ud._ase.name))
            del resume_bytes
        # check if all operations completed
        if offsets is None and ud.all_operations_completed:
            # finalize file
            self._finalize_file(ud)
            # accounting
            with self._upload_lock:
                if ud.entity.is_encrypted:
                    self._ud_map.pop(ud.unique_id)
                self._upload_set.remove(ud.unique_id)
                self._upload_sofar += 1
            return
        # if nothing to upload, re-enqueue for finalization
        if offsets is None:
            self._upload_queue.put(ud)
            return

        # TODO encryption

        # encrypt if necessary
        if ud.entity.is_encrypted:
            # send iv through hmac
            ud.hmac_data(ud.current_iv)
            # encrypt data
            if self._crypto_offload is not None:
                self._crypto_offload.add_encrypt_chunk(
                    str(ud.local_path.absolute_path), offsets,
                    ud.entity.encryption_metadata.symmetric_key,
                    ud.current_iv)
                # encrypted data will be retrieved from a temp file once
                # retrieved from crypto queue
                return
            else:
                # read data from file and encrypt
                data = ud.read_data(offsets)
                encdata = blobxfer.operations.crypto.aes_cbc_encrypt_data(
                    ud.entity.encryption_metadata.symmetric_key,
                    ud.current_iv, data, offsets.pad)
                # send encrypted data through hmac
                ud.hmac_data(encdata)
                data = encdata
                # TODO save last 16 encrypted bytes for next IV
        else:
            data = ud.read_data(offsets)
        # re-enqueue for other threads to upload
        self._upload_queue.put(ud)
        # add data to transfer queue
        with self._transfer_lock:
            self._transfer_set.add(
                self._create_unique_transfer_id(
                    ud.local_path, ud.entity, offsets))
        self._transfer_queue.put((ud, ud.entity, offsets, data))
        # iterate replicas
        if blobxfer.util.is_not_empty(ud.entity.replica_targets):
            for ase in ud.entity.replica_targets:
                with self._transfer_lock:
                    self._transfer_set.add(
                        self._create_unique_transfer_id(
                            ud.local_path, ase, offsets))
                self._transfer_queue.put((ud, ase, offsets, data))

    def _finalize_file(self, ud):
        metadata = ud.generate_metadata()
        # put block list for non one-shot block blobs
        if ud.requires_put_block_list:
            if ud.must_compute_md5:
                digest = blobxfer.util.base64_encode_as_string(ud.md5.digest())
            else:
                digest = None
            blobxfer.operations.azure.blob.block.put_block_list(
                ud.entity, ud.last_block_num, digest, metadata,
                timeout=self._general_options.timeout_sec)
            if blobxfer.util.is_not_empty(ud.entity.replica_targets):
                for ase in ud.entity.replica_targets:
                    blobxfer.operations.azure.blob.block.put_block_list(
                        ase, ud.last_block_num, digest, metadata,
                        timeout=self._general_options.timeout_sec)
        # page blob finalization
        if ud.remote_is_page_blob:
            # set md5 page blob property if required
            if ud.requires_non_encrypted_md5_put:
                digest = blobxfer.util.base64_encode_as_string(ud.md5.digest())
                blobxfer.operations.azure.blob.page.set_blob_md5(
                    ud.entity, digest,
                    timeout=self._general_options.timeout_sec)
                if blobxfer.util.is_not_empty(ud.entity.replica_targets):
                    for ase in ud.entity.replica_targets:
                        blobxfer.operations.azure.blob.page.set_blob_md5(
                            ase, digest,
                            timeout=self._general_options.timeout_sec)
            # set metadata if needed
            if blobxfer.util.is_not_empty(metadata):
                blobxfer.operations.azure.blob.page.set_blob_metadata(
                    ud.entity, metadata,
                    timeout=self._general_options.timeout_sec)
                if blobxfer.util.is_not_empty(ud.entity.replica_targets):
                    for ase in ud.entity.replica_targets:
                        blobxfer.operations.azure.blob.page.set_blob_metadata(
                            ase, metadata,
                            timeout=self._general_options.timeout_sec)
        # azure file finalization
        if ud.remote_is_file:
            # set md5 file property if required
            if ud.requires_non_encrypted_md5_put:
                digest = blobxfer.util.base64_encode_as_string(ud.md5.digest())
                blobxfer.operations.azure.file.set_file_md5(
                    ud.entity, digest,
                    timeout=self._general_options.timeout_sec)
                if blobxfer.util.is_not_empty(ud.entity.replica_targets):
                    for ase in ud.entity.replica_targets:
                        blobxfer.operations.azure.file.set_file_md5(
                            ase, digest,
                            timeout=self._general_options.timeout_sec)
            # set file metadata if needed
            if blobxfer.util.is_not_empty(metadata):
                blobxfer.operations.azure.file.set_file_metadata(
                    ud.entity, metadata,
                    timeout=self._general_options.timeout_sec)
                if blobxfer.util.is_not_empty(ud.entity.replica_targets):
                    for ase in ud.entity.replica_targets:
                        blobxfer.operations.azure.file.set_file_metadata(
                            ase, metadata,
                            timeout=self._general_options.timeout_sec)

    def _cleanup_temporary_files(self):
        # type: (Uploader) -> None
        """Cleanup temporary files in case of an exception or interrupt.
        This function is not thread-safe.
        :param Uploader self: this
        """
        # iterate through dd map and cleanup files
        for key in self._ud_map:
            dd = self._ud_map[key]
            try:
                dd.cleanup_all_temporary_files()
            except Exception as e:
                logger.exception(e)

    def _get_destination_paths(self):
        # type: (Uploader) ->
        #        Tuple[blobxfer.operations.azure.StorageAccount, str, str]
        """Get destination paths
        :param Uploader self: this
        :rtype: tuple
        :return: (storage account, container, name)
        """
        for dst in self._spec.destinations:
            for dpath in dst.paths:
                sdpath = str(dpath)
                cont, dir = blobxfer.util.explode_azure_path(sdpath)
                sa = self._creds.get_storage_account(
                    dst.lookup_storage_account(sdpath))
                yield sa, cont, dir, dpath

    def _delete_extraneous_files(self):
        # type: (Uploader) -> None
        """Delete extraneous files on the remote
        :param Uploader self: this
        """
        if not self._spec.options.delete_extraneous_destination:
            return
        # list blobs for all destinations
        checked = set()
        deleted = 0
        print(self._delete_exclude)
        for sa, container, _, _ in self._get_destination_paths():
            key = ';'.join((sa.name, sa.endpoint, container))
            if key in checked:
                continue
            logger.debug(
                'attempting to delete extraneous blobs/files from: {}'.format(
                    key))
            if (self._spec.options.mode ==
                    blobxfer.models.azure.StorageModes.File):
                files = blobxfer.operations.azure.file.list_all_files(
                    sa.file_client, container,
                    timeout=self._general_options.timeout_sec)
                for file in files:
                    id = self._create_deletion_id(
                        sa.file_client, container, file)
                    print(id)
                    if id not in self._delete_exclude:
                        blobxfer.operations.azure.file.delete_file(
                            sa.file_client, container, file,
                            timeout=self._general_options.timeout_sec)
                        deleted += 1
            else:
                blobs = blobxfer.operations.azure.blob.list_all_blobs(
                    sa.block_blob_client, container,
                    timeout=self._general_options.timeout_sec)
                for blob in blobs:
                    id = self._create_deletion_id(
                        sa.block_blob_client, container, blob.name)
                    if id not in self._delete_exclude:
                        blobxfer.operations.azure.blob.delete_blob(
                            sa.block_blob_client, container, blob.name,
                            timeout=self._general_options.timeout_sec)
                        deleted += 1
            checked.add(key)
        logger.info('deleted {} extraneous blobs/files'.format(deleted))

    def _check_upload_conditions(self, local_path, rfile):
        # type: (Uploader, blobxfer.models.upload.LocalPath,
        #        blobxfer.models.azure.StorageEntity) -> UploadAction
        """Check for upload conditions
        :param Uploader self: this
        :param blobxfer.models.LocalPath local_path: local path
        :param blobxfer.models.azure.StorageEntity rfile: remote file
        :rtype: UploadAction
        :return: upload action
        """
        lpath = local_path.absolute_path
        # check if local file still exists
        if not lpath.exists():
            return UploadAction.Skip
        # if remote file doesn't exist, upload
        if rfile is None:
            return UploadAction.Upload
        # check overwrite option
        if not self._spec.options.overwrite:
            logger.info(
                'not overwriting remote file: {} (local: {})'.format(
                    rfile.path, lpath))
            return UploadAction.Skip
        # check skip on options, MD5 match takes priority
        if (self._spec.skip_on.md5_match and
                blobxfer.util.is_not_empty(rfile.md5)):
            return UploadAction.CheckMd5
        # if neither of the remaining skip on actions are activated, upload
        if (not self._spec.skip_on.filesize_match and
                not self._spec.skip_on.lmt_ge):
            return UploadAction.Upload
        # check skip on file size match
        ul_fs = None
        if self._spec.skip_on.filesize_match:
            lsize = local_path.size
            if rfile.mode == blobxfer.models.azure.StorageModes.Page:
                lsize = blobxfer.util.page_align_content_length(lsize)
            if rfile.size == lsize:
                ul_fs = False
            else:
                ul_fs = True
        # check skip on lmt ge
        ul_lmt = None
        if self._spec.skip_on.lmt_ge:
            mtime = blobxfer.util.datetime_from_timestamp(local_path.lmt)
            if rfile.lmt >= mtime:
                ul_lmt = False
            else:
                ul_lmt = True
        # upload if either skip on mismatch is True
        if ul_fs or ul_lmt:
            return UploadAction.Upload
        else:
            return UploadAction.Skip

    def _check_for_existing_remote(self, sa, cont, name):
        if self._spec.options.mode == blobxfer.models.azure.StorageModes.File:
            fp = blobxfer.operations.azure.file.get_file_properties(
                sa.file_client, cont, name,
                timeout=self._general_options.timeout_sec)
        else:
            fp = blobxfer.operations.azure.blob.get_blob_properties(
                sa.block_blob_client, cont, name, self._spec.options.mode,
                timeout=self._general_options.timeout_sec)
        if fp is not None:
            if blobxfer.models.crypto.EncryptionMetadata.\
                    encryption_metadata_exists(fp.metadata):
                ed = blobxfer.models.crypto.EncryptionMetadata()
                ed.convert_from_json(fp.metadata, fp.name, None)
            else:
                ed = None
            ase = blobxfer.models.azure.StorageEntity(cont, ed)
            if (self._spec.options.mode ==
                    blobxfer.models.azure.StorageModes.File):
                ase.populate_from_file(sa, fp, name)
            else:
                ase.populate_from_blob(sa, fp, name)
        else:
            ase = None
        return ase

    def _generate_destination_for_source(self, local_path):
        # type: (Uploader, blobxfer.models.upload.LocalSourcePath) -> ???
        """Generate entities for source path
        :param Uploader self: this
        :param blobxfer.models.upload.LocalSourcePath local_path: local path
        """
        # construct stripped destination path
        spath = local_path.relative_path
        # apply strip components
        if self._spec.options.strip_components > 0:
            _rparts = local_path.relative_path.parts
            _strip = min(
                (len(_rparts) - 1, self._spec.options.strip_components)
            )
            if _strip > 0:
                spath = pathlib.Path(*_rparts[_strip:])
        # create a storage entity for each destination
        for sa, cont, name, dpath in self._get_destination_paths():
            # apply rename
            if not self._spec.options.rename:
                name = str(spath / name)
            if blobxfer.util.is_none_or_empty(name):
                raise ValueError(
                    ('invalid destination, must specify a container or '
                     'fileshare and remote file name: {}').format(dpath))
            # do not check for existing remote right now if striped
            # vectored io mode
            if (self._spec.options.vectored_io.distribution_mode ==
                    blobxfer.models.upload.
                    VectoredIoDistributionMode.Stripe):
                ase = None
            else:
                ase = self._check_for_existing_remote(sa, cont, name)
            if ase is None:
                if self._spec.options.rsa_public_key:
                    ed = blobxfer.models.crypto.EncryptionMetadata()
                else:
                    ed = None
                ase = blobxfer.models.azure.StorageEntity(cont, ed)
                ase.populate_from_local(
                    sa, cont, name, self._spec.options.mode)
            yield sa, ase

    def _create_unique_id(self, src, ase):
        return ';'.join(
            (str(src.absolute_path), ase._client.primary_endpoint, ase.path)
        )

    def _create_unique_transfer_id(self, local_path, ase, offsets):
        return ';'.join(
            (str(local_path.absolute_path), ase._client.primary_endpoint,
             ase.path, str(local_path.view.fd_start), str(offsets.range_start))
        )

    def _create_deletion_id(self, client, container, name):
        return ';'.join(
            (client.primary_endpoint, container, name)
        )

    def append_slice_suffix_to_name(self, name, slice):
        return '{}.bxslice-{}'.format(name, slice)

    def _vectorize_and_bind(self, local_path, dest):
        # type: (Uploader, blobxfer.models.upload.LocalPath,
        #        List[blobxfer.models.azure.StorageEntity]) -> None
        """Vectorize local path to destinations and bind
        :param Uploader self: this
        :param blobxfer.models.LocalPath local_path: local path
        :param list rfile: remote file
        """
        if (self._spec.options.vectored_io.distribution_mode ==
                blobxfer.models.upload.VectoredIoDistributionMode.Stripe):
            # compute total number of slices
            slices = int(math.ceil(
                local_path.total_size /
                self._spec.options.vectored_io.stripe_chunk_size_bytes))
            # check if vectorization is possible
            if slices == 1:
                sa, ase = dest[0]
                action = self._check_upload_conditions(local_path, ase)
                yield action, local_path, ase
                return
            num_dest = len(dest)
            logger.debug(
                '{} slices for vectored out of {} to {} destinations'.format(
                    slices, local_path.absolute_path, num_dest))
            # pre-populate slice map for next pointers
            slice_map = {}
            for i in range(0, slices):
                sa, ase = dest[i % num_dest]
                name = self.append_slice_suffix_to_name(ase.name, i)
                sase = self._check_for_existing_remote(sa, ase.container, name)
                if sase is None:
                    if self._spec.options.rsa_public_key:
                        ed = blobxfer.models.crypto.EncryptionMetadata()
                    else:
                        ed = None
                    sase = blobxfer.models.azure.StorageEntity(
                        ase.container, ed)
                    sase.populate_from_local(
                        sa, ase.container, name, self._spec.options.mode)
                slice_map[i] = sase
            # create new local path to ase mappings
            curr = 0
            for i in range(0, slices):
                start = curr
                end = (
                    curr +
                    self._spec.options.vectored_io.stripe_chunk_size_bytes
                )
                if end > local_path.total_size:
                    end = local_path.total_size
                ase = slice_map[i]
                if i < slices - 1:
                    next_entry = blobxfer.models.metadata.\
                        create_vectored_io_next_entry(slice_map[i+1])
                else:
                    next_entry = None
                lp_slice = blobxfer.models.upload.LocalPath(
                    parent_path=local_path.parent_path,
                    relative_path=local_path.relative_path,
                    view=blobxfer.models.upload.LocalPathView(
                        fd_start=start,
                        fd_end=end,
                        slice_num=i,
                        mode=self._spec.options.vectored_io.distribution_mode,
                        total_slices=slices,
                        next=next_entry,
                    )
                )
                print(lp_slice.view)
                action = self._check_upload_conditions(lp_slice, ase)
                yield action, lp_slice, ase
                curr = end
        elif (self._spec.options.vectored_io.distribution_mode ==
                blobxfer.models.upload.VectoredIoDistributionMode.Replica):
            action_map = {}
            for _, ase in dest:
                action = self._check_upload_conditions(local_path, ase)
                if action not in action_map:
                    action_map[action] = []
                action_map[action].append(ase)
            for action in action_map:
                dst = action_map[action]
                if len(dst) == 1:
                    yield action, local_path, dst[0]
                else:
                    if (action == UploadAction.CheckMd5 or
                            action == UploadAction.Skip):
                        for ase in dst:
                            yield action, local_path, ase
                    else:
                        primary_ase = dst[0]
                        primary_ase.replica_targets.extend(dst[1:])
                        yield action, local_path, primary_ase
        else:
            for _, ase in dest:
                action = self._check_upload_conditions(local_path, ase)
                yield action, local_path, ase

    def _run(self):
        # type: (Uploader) -> None
        """Execute Uploader
        :param Uploader self: this
        """
        # mark start
        self._start_time = blobxfer.util.datetime_now()
        logger.info('blobxfer start time: {0}'.format(self._start_time))
        # initialize resume db if specified
#         if self._general_options.resume_file is not None:
#             self._resume = blobxfer.operations.resume.DownloadResumeManager(
#                 self._general_options.resume_file)
        # initialize MD5 processes
        if ((self._spec.options.store_file_properties.md5 or
             self._spec.skip_on.md5_match) and
                self._general_options.concurrency.md5_processes > 0):
            self._md5_offload = blobxfer.operations.md5.LocalFileMd5Offload(
                num_workers=self._general_options.concurrency.md5_processes)
            self._md5_offload.initialize_check_thread(
                self._check_for_uploads_from_md5)
        # initialize crypto processes
        if self._general_options.concurrency.crypto_processes > 0:
            self._crypto_offload = blobxfer.operations.crypto.CryptoOffload(
                num_workers=self._general_options.concurrency.crypto_processes)
            self._crypto_offload.initialize_check_thread(
                self._check_for_crypto_done)
        # initialize upload threads
        self._initialize_upload_threads()
        self._initialize_transfer_threads()
        # initialize local counters
        nfiles = 0
        total_size = 0
        skipped_files = 0
        skipped_size = 0
        if not self._spec.sources.can_rename() and self._spec.options.rename:
            raise RuntimeError(
                'cannot rename to specified destination with multiple sources')
        # iterate through source paths to upload
        for src in self._spec.sources.files():
            # create a destination array for the source
            dest = [
                (sa, ase) for sa, ase in
                self._generate_destination_for_source(src)
            ]
            for action, lp, ase in self._vectorize_and_bind(src, dest):
                print(lp.parent_path, lp.relative_path, lp.absolute_path, action, ase.container, ase.name)
                print(lp.total_size, lp.size, lp.mode, lp.uid, lp.gid)
                print(self._create_unique_id(lp, ase))
                print('replicas', len(ase.replica_targets) if ase.replica_targets is not None else 'none')
                if self._spec.options.delete_extraneous_destination:
                    self._delete_exclude.add(
                        self._create_deletion_id(
                            ase._client, ase.container, ase.name)
                    )
                if action == UploadAction.Skip:
                    skipped_files += 1
                    skipped_size += ase.size if ase.size is not None else 0
                    continue
                # add to potential upload set
                uid = self._create_unique_id(lp, ase)
                with self._upload_lock:
                    self._upload_set.add(uid)
                if action == UploadAction.CheckMd5:
                    self._pre_md5_skip_on_check(lp, ase)
                elif action == UploadAction.Upload:
                    self._add_to_upload_queue(lp, ase, uid)

                    nfiles += 1
                    total_size += lp.size

        self._upload_total = nfiles - skipped_files
        self._upload_bytes_total = total_size - skipped_size
        upload_size_mib = self._upload_bytes_total / blobxfer.util.MEGABYTE
        # set remote files processed
        with self._md5_meta_lock:
            self._all_local_files_processed = True
        logger.debug(
            ('{0} local files processed, waiting for upload completion '
             'of {1:.4f} MiB').format(nfiles, upload_size_mib))
        del nfiles
        del total_size
        del skipped_files
        del skipped_size
        # wait for uploads to complete
        self._wait_for_upload_threads(terminate=False)
        self._wait_for_transfer_threads(terminate=False)
        end_time = blobxfer.util.datetime_now()
        # update progress bar
        self._update_progress_bar()
        # check for exceptions
        if len(self._exceptions) > 0:
            logger.error('exceptions encountered while uploading')
            # raise the first one
            raise self._exceptions[0]
        # check for mismatches
        if (self._upload_sofar != self._upload_total or
                self._upload_bytes_sofar != self._upload_bytes_total):
            raise RuntimeError(
                'upload mismatch: [count={}/{} bytes={}/{}]'.format(
                    self._upload_sofar, self._upload_total,
                    self._upload_bytes_sofar, self._upload_bytes_total))
        # delete all remaining local files not accounted for if
        # delete extraneous enabled
        self._delete_extraneous_files()
        # delete resume file if we've gotten this far
        if self._resume is not None:
            self._resume.delete()
        # output throughput
        if self._upload_start_time is not None:
            ultime = (end_time - self._upload_start_time).total_seconds()
            mibps = upload_size_mib / ultime
            logger.info(
                ('elapsed upload + verify time and throughput: {0:.3f} sec, '
                 '{1:.4f} Mbps ({2:.3f} MiB/s)').format(
                     ultime, mibps * 8, mibps))
        end_time = blobxfer.util.datetime_now()
        logger.info('blobxfer end time: {0} (elapsed: {1:.3f} sec)'.format(
            end_time, (end_time - self._start_time).total_seconds()))

    def start(self):
        # type: (Uploader) -> None
        """Start the Uploader
        :param Uploader self: this
        """
        try:
            blobxfer.operations.progress.output_parameters(
                self._general_options, self._spec)
            self._run()
        except (KeyboardInterrupt, Exception) as ex:
            if isinstance(ex, KeyboardInterrupt):
                logger.info(
                    'KeyboardInterrupt detected, force terminating '
                    'processes and threads (this may take a while)...')
            try:
                self._wait_for_transfer_threads(terminate=True)
                self._wait_for_upload_threads(terminate=True)
            finally:
                self._cleanup_temporary_files()
            raise
        finally:
            # shutdown processes
            if self._md5_offload is not None:
                self._md5_offload.finalize_processes()
            if self._crypto_offload is not None:
                self._crypto_offload.finalize_processes()
            # close resume file
            if self._resume is not None:
                self._resume.close()
