# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import (
    Any,
    Dict,
    Optional,
    Sequence,
    Tuple,
    TYPE_CHECKING,
)
from google.api_core import retry as retries

from google.cloud.bigtable.data.execute_query._byte_cursor import _ByteCursor
from google.cloud.bigtable.data._helpers import (
    _attempt_timeout_generator,
    _retry_exception_factory,
)
from google.cloud.bigtable.data.exceptions import InvalidExecuteQueryResponse
from google.cloud.bigtable.data.execute_query.values import QueryResultRow
from google.cloud.bigtable.data.execute_query.metadata import Metadata, ProtoMetadata
from google.cloud.bigtable.data.execute_query._reader import (
    _QueryResultRowReader,
    _Reader,
)
from google.cloud.bigtable_v2.types.bigtable import (
    ExecuteQueryRequest as ExecuteQueryRequestPB,
)

from google.cloud.bigtable.data._cross_sync import CrossSync

if TYPE_CHECKING:
    if CrossSync.is_async:
        from google.cloud.bigtable.data import BigtableDataClientAsync as DataClientType
    else:
        from google.cloud.bigtable.data import BigtableDataClient as DataClientType

__CROSS_SYNC_OUTPUT__ = (
    "google.cloud.bigtable.data.execute_query._sync_autogen.execute_query_iterator"
)


@CrossSync.convert_class(sync_name="ExecuteQueryIterator")
class ExecuteQueryIteratorAsync:
    @CrossSync.convert(
        docstring_format_vars={
            "NO_LOOP": (
                "RuntimeError: if the instance is not created within an async event loop context.",
                "None",
            ),
            "TASK_OR_THREAD": ("asyncio Tasks", "threads"),
        }
    )
    def __init__(
        self,
        client: DataClientType,
        instance_id: str,
        app_profile_id: Optional[str],
        request_body: Dict[str, Any],
        attempt_timeout: float | None,
        operation_timeout: float,
        req_metadata: Sequence[Tuple[str, str]] = (),
        retryable_excs: Sequence[type[Exception]] = (),
    ) -> None:
        """
        Collects responses from ExecuteQuery requests and parses them into QueryResultRows.

        It is **not thread-safe**. It should not be used by multiple {TASK_OR_THREAD}.

        Args:
            client: bigtable client
            instance_id: id of the instance on which the query is executed
            request_body: dict representing the body of the ExecuteQueryRequest
            attempt_timeout: the time budget for an individual network request, in seconds.
                If it takes longer than this time to complete, the request will be cancelled with
                a DeadlineExceeded exception, and a retry will be attempted.
            operation_timeout: the time budget for the entire operation, in seconds.
                Failed requests will be retried within the budget
            req_metadata: metadata used while sending the gRPC request
            retryable_excs: a list of errors that will be retried if encountered.
        Raises:
            {NO_LOOP}
        """
        self._table_name = None
        self._app_profile_id = app_profile_id
        self._client = client
        self._instance_id = instance_id
        self._byte_cursor = _ByteCursor[ProtoMetadata]()
        self._reader: _Reader[QueryResultRow] = _QueryResultRowReader(self._byte_cursor)
        self._result_generator = self._next_impl()
        self._register_instance_task = None
        self._is_closed = False
        self._request_body = request_body
        self._attempt_timeout_gen = _attempt_timeout_generator(
            attempt_timeout, operation_timeout
        )
        self._stream = CrossSync.retry_target_stream(
            self._make_request_with_resume_token,
            retries.if_exception_type(*retryable_excs),
            retries.exponential_sleep_generator(0.01, 60, multiplier=2),
            operation_timeout,
            exception_factory=_retry_exception_factory,
        )
        self._req_metadata = req_metadata
        try:
            self._register_instance_task = CrossSync.create_task(
                self._client._register_instance,
                instance_id,
                self,
                sync_executor=self._client._executor,
            )
        except RuntimeError as e:
            raise RuntimeError(
                f"{self.__class__.__name__} must be created within an async event loop context."
            ) from e

    @property
    def is_closed(self) -> bool:
        """Returns True if the iterator is closed, False otherwise."""
        return self._is_closed

    @property
    def app_profile_id(self) -> Optional[str]:
        """Returns the app_profile_id of the iterator."""
        return self._app_profile_id

    @property
    def table_name(self) -> Optional[str]:
        """Returns the table_name of the iterator."""
        return self._table_name

    @CrossSync.convert
    async def _make_request_with_resume_token(self):
        """
        perfoms the rpc call using the correct resume token.
        """
        resume_token = self._byte_cursor.prepare_for_new_request()
        request = ExecuteQueryRequestPB(
            {
                **self._request_body,
                "resume_token": resume_token,
            }
        )
        return await self._client._gapic_client.execute_query(
            request,
            timeout=next(self._attempt_timeout_gen),
            metadata=self._req_metadata,
            retry=None,
        )

    @CrossSync.convert(replace_symbols={"__anext__": "__next__"})
    async def _fetch_metadata(self) -> None:
        """
        If called before the first response was recieved, the first response
        is retrieved as part of this call.
        """
        if self._byte_cursor.metadata is None:
            metadata_msg = await self._stream.__anext__()
            self._byte_cursor.consume_metadata(metadata_msg)

    @CrossSync.convert
    async def _next_impl(self) -> CrossSync.Iterator[QueryResultRow]:
        """
        Generator wrapping the response stream which parses the stream results
        and returns full `QueryResultRow`s.
        """
        await self._fetch_metadata()

        async for response in self._stream:
            try:
                bytes_to_parse = self._byte_cursor.consume(response)
                if bytes_to_parse is None:
                    continue

                results = self._reader.consume(bytes_to_parse)
                if results is None:
                    continue

            except ValueError as e:
                raise InvalidExecuteQueryResponse(
                    "Invalid ExecuteQuery response received"
                ) from e

            for result in results:
                yield result
        await self.close()

    @CrossSync.convert(sync_name="__next__", replace_symbols={"__anext__": "__next__"})
    async def __anext__(self) -> QueryResultRow:
        if self._is_closed:
            raise CrossSync.StopIteration
        return await self._result_generator.__anext__()

    @CrossSync.convert(sync_name="__iter__")
    def __aiter__(self):
        return self

    @CrossSync.convert
    async def metadata(self) -> Optional[Metadata]:
        """
        Returns query metadata from the server or None if the iterator was
        explicitly closed.
        """
        if self._is_closed:
            return None
        # Metadata should be present in the first response in a stream.
        if self._byte_cursor.metadata is None:
            try:
                await self._fetch_metadata()
            except CrossSync.StopIteration:
                return None
        return self._byte_cursor.metadata

    @CrossSync.convert
    async def close(self) -> None:
        """
        Cancel all background tasks. Should be called all rows were processed.
        """
        if self._is_closed:
            return
        self._is_closed = True
        if self._register_instance_task is not None:
            self._register_instance_task.cancel()
        await self._client._remove_instance_registration(self._instance_id, self)
