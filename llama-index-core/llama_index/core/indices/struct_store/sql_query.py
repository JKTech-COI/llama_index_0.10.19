"""Default query for SQLStructStoreIndex."""

import logging
from abc import abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from llama_index.core.base.base_query_engine import BaseQueryEngine
from llama_index.core.base.response.schema import Response
from llama_index.core.callbacks import CallbackManager
from llama_index.core.indices.struct_store.container_builder import (
    SQLContextContainerBuilder,
)
from llama_index.core.indices.struct_store.sql import SQLStructStoreIndex
from llama_index.core.indices.struct_store.sql_retriever import (
    NLSQLRetriever,
    SQLParserMode,
)
from llama_index.core.llms.llm import LLM
from llama_index.core.objects.base import ObjectRetriever
from llama_index.core.objects.table_node_mapping import SQLTableSchema
from llama_index.core.prompts import BasePromptTemplate, PromptTemplate
from llama_index.core.prompts.default_prompts import (
    DEFAULT_TEXT_TO_SQL_PGVECTOR_PROMPT,
    DEFAULT_TEXT_TO_SQL_PROMPT,
    DEFAULT_REFINE_PROMPT,
)
from llama_index.core.prompts.mixin import PromptDictType, PromptMixinType
from llama_index.core.prompts.prompt_type import PromptType
from llama_index.core.response_synthesizers import (
    get_response_synthesizer,
)
from llama_index.core.schema import QueryBundle
from llama_index.core.service_context import ServiceContext
from llama_index.core.settings import (
    Settings,
    callback_manager_from_settings_or_context,
    llm_from_settings_or_context,
)
from llama_index.core.utilities.sql_wrapper import SQLDatabase
from sqlalchemy import Table

logger = logging.getLogger(__name__)


# **NOTE**: deprecated (for older versions of sql query engine)
DEFAULT_RESPONSE_SYNTHESIS_PROMPT_TMPL = (
    "Given an input question, synthesize a response from the query results.\n"
    "Query: {query_str}\n"
    "SQL: {sql_query}\n"
    "SQL Response: {sql_response_str}\n"
    "Response: "
)
DEFAULT_RESPONSE_SYNTHESIS_PROMPT = PromptTemplate(
    DEFAULT_RESPONSE_SYNTHESIS_PROMPT_TMPL,
    prompt_type=PromptType.SQL_RESPONSE_SYNTHESIS,
)

# **NOTE**: newer version of sql query engine
DEFAULT_RESPONSE_SYNTHESIS_PROMPT_TMPL_V2 = (
    "Given an input question, synthesize a response from the query results.\n"
    "Query: {query_str}\n"
    "SQL: {sql_query}\n"
    "SQL Response: {context_str}\n"
    "Response: "
)
DEFAULT_RESPONSE_SYNTHESIS_PROMPT_V2 = PromptTemplate(
    DEFAULT_RESPONSE_SYNTHESIS_PROMPT_TMPL_V2,
    prompt_type=PromptType.SQL_RESPONSE_SYNTHESIS_V2,
)


class SQLStructStoreQueryEngine(BaseQueryEngine):
    """GPT SQL query engine over a structured database.

    NOTE: deprecated in favor of SQLTableRetriever, kept for backward compatibility.

    Runs raw SQL over a SQLStructStoreIndex. No LLM calls are made here.
    NOTE: this query cannot work with composed indices - if the index
    contains subindices, those subindices will not be queried.
    """

    def __init__(
        self,
        index: SQLStructStoreIndex,
        sql_context_container: Optional[SQLContextContainerBuilder] = None,
        sql_only: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        self._sql_database = index.sql_database
        self._sql_context_container = (
            sql_context_container or index.sql_context_container
        )
        self._sql_only = sql_only
        super().__init__(
            callback_manager=callback_manager_from_settings_or_context(
                Settings, index.service_context
            )
        )

    def _get_prompt_modules(self) -> PromptMixinType:
        """Get prompt modules."""
        return {}

    def _run_with_sql_only_check(
        self, sql_query_str: str
    ) -> Tuple[str, Dict[str, Any]]:
        """Don't run sql if sql_only is true, else continue with normal path."""
        if self._sql_only:
            metadata: Dict[str, Any] = {}
            raw_response_str = sql_query_str
        else:
            raw_response_str, metadata = self._sql_database.run_sql(sql_query_str)

        return raw_response_str, metadata

    def _query(self, query_bundle: QueryBundle) -> Response:
        """Answer a query."""
        # NOTE: override query method in order to fetch the right results.
        # NOTE: since the query_str is a SQL query, it doesn't make sense
        # to use ResponseBuilder anywhere.
        response_str, metadata = self._run_with_sql_only_check(query_bundle.query_str)
        return Response(response=response_str, metadata=metadata)

    async def _aquery(self, query_bundle: QueryBundle) -> Response:
        return self._query(query_bundle)


class NLStructStoreQueryEngine(BaseQueryEngine):
    """GPT natural language query engine over a structured database.

    NOTE: deprecated in favor of SQLTableRetriever, kept for backward compatibility.

    Given a natural language query, we will extract the query to SQL.
    Runs raw SQL over a SQLStructStoreIndex. No LLM calls are made during
    the SQL execution.

    NOTE: this query cannot work with composed indices - if the index
    contains subindices, those subindices will not be queried.

    Args:
        index (SQLStructStoreIndex): A SQL Struct Store Index
        text_to_sql_prompt (Optional[BasePromptTemplate]): A Text to SQL
            BasePromptTemplate to use for the query.
            Defaults to DEFAULT_TEXT_TO_SQL_PROMPT.
        context_query_kwargs (Optional[dict]): Keyword arguments for the
            context query. Defaults to {}.
        synthesize_response (bool): Whether to synthesize a response from the
            query results. Defaults to True.
        sql_only (bool) : Whether to get only sql and not the sql query result.
            Default to False.
        response_synthesis_prompt (Optional[BasePromptTemplate]): A
            Response Synthesis BasePromptTemplate to use for the query. Defaults to
            DEFAULT_RESPONSE_SYNTHESIS_PROMPT.
    """

    def __init__(
        self,
        index: SQLStructStoreIndex,
        text_to_sql_prompt: Optional[BasePromptTemplate] = None,
        context_query_kwargs: Optional[dict] = None,
        synthesize_response: bool = True,
        response_synthesis_prompt: Optional[BasePromptTemplate] = None,
        sql_only: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        self._index = index
        self._llm = llm_from_settings_or_context(Settings, index.service_context)
        self._sql_database = index.sql_database
        self._sql_context_container = index.sql_context_container
        self._service_context = index.service_context
        self._ref_doc_id_column = index.ref_doc_id_column

        self._text_to_sql_prompt = text_to_sql_prompt or DEFAULT_TEXT_TO_SQL_PROMPT
        self._response_synthesis_prompt = (
            response_synthesis_prompt or DEFAULT_RESPONSE_SYNTHESIS_PROMPT
        )
        self._context_query_kwargs = context_query_kwargs or {}
        self._synthesize_response = synthesize_response
        self._sql_only = sql_only
        super().__init__(
            callback_manager=callback_manager_from_settings_or_context(
                Settings, index.service_context
            )
        )

    @property
    def service_context(self) -> Optional[ServiceContext]:
        """Get service context."""
        return self._service_context

    def _get_prompt_modules(self) -> PromptMixinType:
        """Get prompt modules."""
        return {}

    def _parse_response_to_sql(self, response: str) -> str:
        """Parse response to SQL."""
        # Find and remove SQLResult part
        sql_result_start = response.find("SQLResult:")
        if sql_result_start != -1:
            response = response[:sql_result_start]
        return response.strip()

    def _get_table_context(self, query_bundle: QueryBundle) -> str:
        """Get table context.

        Get tables schema + optional context as a single string. Taken from
        SQLContextContainer.

        """
        if self._sql_context_container.context_str is not None:
            tables_desc_str = self._sql_context_container.context_str
        else:
            table_desc_list = []
            context_dict = self._sql_context_container.context_dict
            if context_dict is None:
                raise ValueError(
                    "context_dict must be provided. There is currently no "
                    "table context."
                )
            for table_desc in context_dict.values():
                table_desc_list.append(table_desc)
            tables_desc_str = "\n\n".join(table_desc_list)

        return tables_desc_str

    def _run_with_sql_only_check(self, sql_query_str: str) -> Tuple[str, Dict]:
        """Don't run sql if sql_only is true, else continue with normal path."""
        if self._sql_only:
            metadata: Dict[str, Any] = {}
            raw_response_str = sql_query_str
        else:
            raw_response_str, metadata = self._sql_database.run_sql(sql_query_str)

        return raw_response_str, metadata

    def _query(self, query_bundle: QueryBundle) -> Response:
        """Answer a query."""
        table_desc_str = self._get_table_context(query_bundle)
        logger.info(f"> Table desc str: {table_desc_str}")

        response_str = self._llm.predict(
            self._text_to_sql_prompt,
            query_str=query_bundle.query_str,
            schema=table_desc_str,
            dialect=self._sql_database.dialect,
        )

        sql_query_str = self._parse_response_to_sql(response_str)
        # assume that it's a valid SQL query
        logger.debug(f"> Predicted SQL query: {sql_query_str}")

        raw_response_str, metadata = self._run_with_sql_only_check(sql_query_str)

        metadata["sql_query"] = sql_query_str

        if self._synthesize_response:
            response_str = self._llm.predict(
                self._response_synthesis_prompt,
                query_str=query_bundle.query_str,
                sql_query=sql_query_str,
                sql_response_str=raw_response_str,
            )
        else:
            response_str = raw_response_str

        return Response(response=response_str, metadata=metadata)

    async def _aquery(self, query_bundle: QueryBundle) -> Response:
        """Answer a query."""
        table_desc_str = self._get_table_context(query_bundle)
        logger.info(f"> Table desc str: {table_desc_str}")

        response_str = await self._llm.apredict(
            self._text_to_sql_prompt,
            query_str=query_bundle.query_str,
            schema=table_desc_str,
            dialect=self._sql_database.dialect,
        )

        sql_query_str = self._parse_response_to_sql(response_str)
        # assume that it's a valid SQL query
        logger.debug(f"> Predicted SQL query: {sql_query_str}")

        response_str, metadata = self._run_with_sql_only_check(sql_query_str)
        metadata["sql_query"] = sql_query_str
        return Response(response=response_str, metadata=metadata)


def _validate_prompt(
    custom_prompt: BasePromptTemplate,
    default_prompt: BasePromptTemplate,
) -> None:
    """Validate prompt."""
    if custom_prompt.template_vars != default_prompt.template_vars:
        raise ValueError(
            "custom_prompt must have the following template variables: "
            f"{default_prompt.template_vars}"
        )
        
#Abhijit
import tiktoken
def num_tokens_from_string(string: str, encoding_name='cl100k_base') -> int:
    """Returns the number of tokens in a text string."""
    print("Counting number of tokens generated by the agent")
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens
#

class BaseSQLTableQueryEngine(BaseQueryEngine):
    def __init__(
        self,
        llm: Optional[LLM] = None,
        synthesize_response: bool = True,
        response_synthesis_prompt: Optional[BasePromptTemplate] = None,
        callback_manager: Optional[CallbackManager] = None,
        refine_synthesis_prompt: Optional[BasePromptTemplate] = None,
        verbose: bool = False,
        # deprecated
        service_context: Optional[ServiceContext] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        self._service_context = service_context
        self._llm = llm or llm_from_settings_or_context(Settings, service_context)
        if callback_manager is not None:
            self._llm.callback_manager = callback_manager

        self._response_synthesis_prompt = (
            response_synthesis_prompt or DEFAULT_RESPONSE_SYNTHESIS_PROMPT_V2
        )
        self._refine_synthesis_prompt = refine_synthesis_prompt or DEFAULT_REFINE_PROMPT

        # do some basic prompt validation
        _validate_prompt(
            self._response_synthesis_prompt, DEFAULT_RESPONSE_SYNTHESIS_PROMPT_V2
        )
        _validate_prompt(self._refine_synthesis_prompt, DEFAULT_REFINE_PROMPT)

        self._synthesize_response = synthesize_response
        self._verbose = verbose
        super().__init__(
            callback_manager=callback_manager
            or callback_manager_from_settings_or_context(Settings, service_context),
            **kwargs,
        )

    def _get_prompts(self) -> Dict[str, Any]:
        """Get prompts."""
        return {"response_synthesis_prompt": self._response_synthesis_prompt}

    def _update_prompts(self, prompts: PromptDictType) -> None:
        """Update prompts."""
        if "response_synthesis_prompt" in prompts:
            self._response_synthesis_prompt = prompts["response_synthesis_prompt"]

    def _get_prompt_modules(self) -> PromptMixinType:
        """Get prompt modules."""
        return {"sql_retriever": self.sql_retriever}

    @property
    @abstractmethod
    def sql_retriever(self) -> NLSQLRetriever:
        """Get SQL retriever."""

    @property
    def service_context(self) -> Optional[ServiceContext]:
        """Get service context."""
        return self._service_context

    def _query(self, query_bundle: QueryBundle) -> Response:
        """Answer a query."""
        retrieved_nodes, metadata = self.sql_retriever.retrieve_with_metadata(
            query_bundle
        )

        sql_query_str = metadata["sql_query"]
        if self._synthesize_response:
            partial_synthesis_prompt = self._response_synthesis_prompt.partial_format(
                sql_query=sql_query_str,
            )
            response_synthesizer = get_response_synthesizer(
                llm=self._llm,
                callback_manager=self.callback_manager,
                text_qa_template=partial_synthesis_prompt,
                refine_template=self._refine_synthesis_prompt,
                verbose=self._verbose,
            )
            # response = response_synthesizer.synthesize(
            #     query=query_bundle.query_str,
            #     nodes=retrieved_nodes,
            # )
            #Abhijit
            txt = ''
            for node in retrieved_nodes:
                txt += node.text +' '
            # print(f"Complete text: {txt}")
            no_tokens = num_tokens_from_string(txt)
            print(f"Number of tokens generated is {no_tokens}")

            if no_tokens > 10000:
                print("^^^^^Below query executed and query output has been saved for analysis. \n"+ sql_query_str)
                try:
                    from llama_index.core.schema import NodeWithScore
                    node = retrieved_nodes[0].node
                    node.text =  "Below query executed and query output has been saved for analysis. \n"+ sql_query_str
                    node = NodeWithScore(node=node)      
                    response = response_synthesizer.synthesize(
                        query=query_bundle.query_str,
                        nodes=[node],
                    )
                except Exception as ae:
                    print(f"^^^^^Occured below execption:\n {ae}")

            else:
                response = response_synthesizer.synthesize(
                    query=query_bundle.query_str,
                    nodes=retrieved_nodes,
                )
            #
            cast(Dict, response.metadata).update(metadata)
            return cast(Response, response)
        else:
            response_str = "\n".join([node.node.text for node in retrieved_nodes])
            return Response(response=response_str, metadata=metadata)

    async def _aquery(self, query_bundle: QueryBundle) -> Response:
        """Answer a query."""
        retrieved_nodes, metadata = await self.sql_retriever.aretrieve_with_metadata(
            query_bundle
        )

        sql_query_str = metadata["sql_query"]
        if self._synthesize_response:
            partial_synthesis_prompt = self._response_synthesis_prompt.partial_format(
                sql_query=sql_query_str,
            )

            response_synthesizer = get_response_synthesizer(
                llm=self._llm,
                callback_manager=self.callback_manager,
                text_qa_template=partial_synthesis_prompt,
                refine_template=self._refine_synthesis_prompt,
            )
            response = await response_synthesizer.asynthesize(
                query=query_bundle.query_str,
                nodes=retrieved_nodes,
            )
            cast(Dict, response.metadata).update(metadata)
            return cast(Response, response)
        else:
            response_str = "\n".join([node.node.text for node in retrieved_nodes])
            return Response(response=response_str, metadata=metadata)


class NLSQLTableQueryEngine(BaseSQLTableQueryEngine):
    """
    Natural language SQL Table query engine.

    Read NLStructStoreQueryEngine's docstring for more info on NL SQL.
    """

    def __init__(
        self,
        sql_database: SQLDatabase,
        llm: Optional[LLM] = None,
        text_to_sql_prompt: Optional[BasePromptTemplate] = None,
        context_query_kwargs: Optional[dict] = None,
        synthesize_response: bool = True,
        response_synthesis_prompt: Optional[BasePromptTemplate] = None,
        refine_synthesis_prompt: Optional[BasePromptTemplate] = None,
        tables: Optional[Union[List[str], List[Table]]] = None,
        service_context: Optional[ServiceContext] = None,
        context_str_prefix: Optional[str] = None,
        sql_only: bool = False,
        callback_manager: Optional[CallbackManager] = None,
        verbose: bool = False,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        # self._tables = tables
        self._sql_retriever = NLSQLRetriever(
            sql_database,
            llm=llm,
            text_to_sql_prompt=text_to_sql_prompt,
            context_query_kwargs=context_query_kwargs,
            tables=tables,
            context_str_prefix=context_str_prefix,
            service_context=service_context,
            sql_only=sql_only,
            callback_manager=callback_manager,
            verbose=verbose,
        )
        super().__init__(
            synthesize_response=synthesize_response,
            response_synthesis_prompt=response_synthesis_prompt,
            refine_synthesis_prompt=refine_synthesis_prompt,
            llm=llm,
            service_context=service_context,
            callback_manager=callback_manager,
            verbose=verbose,
            **kwargs,
        )

    @property
    def sql_retriever(self) -> NLSQLRetriever:
        """Get SQL retriever."""
        return self._sql_retriever


class PGVectorSQLQueryEngine(BaseSQLTableQueryEngine):
    """PGvector SQL query engine.

    A modified version of the normal text-to-SQL query engine because
    we can infer embedding vectors in the sql query.

    NOTE: this is a beta feature

    """

    def __init__(
        self,
        sql_database: SQLDatabase,
        llm: Optional[LLM] = None,
        text_to_sql_prompt: Optional[BasePromptTemplate] = None,
        context_query_kwargs: Optional[dict] = None,
        synthesize_response: bool = True,
        response_synthesis_prompt: Optional[BasePromptTemplate] = None,
        refine_synthesis_prompt: Optional[BasePromptTemplate] = None,
        tables: Optional[Union[List[str], List[Table]]] = None,
        service_context: Optional[ServiceContext] = None,
        context_str_prefix: Optional[str] = None,
        sql_only: bool = False,
        callback_manager: Optional[CallbackManager] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        text_to_sql_prompt = text_to_sql_prompt or DEFAULT_TEXT_TO_SQL_PGVECTOR_PROMPT
        self._sql_retriever = NLSQLRetriever(
            sql_database,
            llm=llm,
            text_to_sql_prompt=text_to_sql_prompt,
            context_query_kwargs=context_query_kwargs,
            tables=tables,
            sql_parser_mode=SQLParserMode.PGVECTOR,
            context_str_prefix=context_str_prefix,
            service_context=service_context,
            sql_only=sql_only,
            callback_manager=callback_manager,
        )
        super().__init__(
            synthesize_response=synthesize_response,
            response_synthesis_prompt=response_synthesis_prompt,
            refine_synthesis_prompt=refine_synthesis_prompt,
            llm=llm,
            service_context=service_context,
            callback_manager=callback_manager,
            **kwargs,
        )

    @property
    def sql_retriever(self) -> NLSQLRetriever:
        """Get SQL retriever."""
        return self._sql_retriever


class SQLTableRetrieverQueryEngine(BaseSQLTableQueryEngine):
    """SQL Table retriever query engine."""

    def __init__(
        self,
        sql_database: SQLDatabase,
        table_retriever: ObjectRetriever[SQLTableSchema],
        llm: Optional[LLM] = None,
        text_to_sql_prompt: Optional[BasePromptTemplate] = None,
        context_query_kwargs: Optional[dict] = None,
        synthesize_response: bool = True,
        response_synthesis_prompt: Optional[BasePromptTemplate] = None,
        refine_synthesis_prompt: Optional[BasePromptTemplate] = None,
        service_context: Optional[ServiceContext] = None,
        context_str_prefix: Optional[str] = None,
        sql_only: bool = False,
        callback_manager: Optional[CallbackManager] = None,
        **kwargs: Any,
    ) -> None:
        """Initialize params."""
        self._sql_retriever = NLSQLRetriever(
            sql_database,
            llm=llm,
            text_to_sql_prompt=text_to_sql_prompt,
            context_query_kwargs=context_query_kwargs,
            table_retriever=table_retriever,
            context_str_prefix=context_str_prefix,
            service_context=service_context,
            sql_only=sql_only,
            callback_manager=callback_manager,
        )
        super().__init__(
            synthesize_response=synthesize_response,
            response_synthesis_prompt=response_synthesis_prompt,
            refine_synthesis_prompt=refine_synthesis_prompt,
            llm=llm,
            service_context=service_context,
            callback_manager=callback_manager,
            **kwargs,
        )

    @property
    def sql_retriever(self) -> NLSQLRetriever:
        """Get SQL retriever."""
        return self._sql_retriever


# legacy
GPTNLStructStoreQueryEngine = NLStructStoreQueryEngine
GPTSQLStructStoreQueryEngine = SQLStructStoreQueryEngine
