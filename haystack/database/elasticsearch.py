import json
import logging
import time
from string import Template
from typing import List, Optional, Union, Dict, Any
from elasticsearch import Elasticsearch
from elasticsearch.helpers import bulk, scan
import numpy as np
from uuid import UUID

from haystack.database.base import BaseDocumentStore, Document, Label
from haystack.indexing.utils import eval_data_from_file

logger = logging.getLogger(__name__)


class ElasticsearchDocumentStore(BaseDocumentStore):
    def __init__(
        self,
        host: str = "localhost",
        port: int = 9200,
        username: str = "",
        password: str = "",
        index: str = "document",
        label_index: str = "label",
        search_fields: Union[str, list] = "text",
        text_field: str = "text",
        name_field: str = "name",
        embedding_field: str = "embedding",
        embedding_dim: int = 768,
        custom_mapping: Optional[dict] = None,
        excluded_meta_data: Optional[list] = None,
        faq_question_field: Optional[str] = None,
        scheme: str = "http",
        ca_certs: bool = False,
        verify_certs: bool = True,
        create_index: bool = True
    ):
        """
        A DocumentStore using Elasticsearch to store and query the documents for our search.

            * Keeps all the logic to store and query documents from Elastic, incl. mapping of fields, adding filters or boosts to your queries, and storing embeddings
            * You can either use an existing Elasticsearch index or create a new one via haystack
            * Retrievers operate on top of this DocumentStore to find the relevant documents for a query

        :param host: url of elasticsearch
        :param port: port of elasticsearch
        :param username: username
        :param password: password
        :param index: Name of index in elasticsearch to use. If not existing yet, we will create one.
        :param search_fields: Name of fields used by ElasticsearchRetriever to find matches in the docs to our incoming query (using elastic's multi_match query), e.g. ["title", "full_text"]
        :param text_field: Name of field that might contain the answer and will therefore be passed to the Reader Model (e.g. "full_text").
                           If no Reader is used (e.g. in FAQ-Style QA) the plain content of this field will just be returned.
        :param name_field: Name of field that contains the title of the the doc
        :param embedding_field: Name of field containing an embedding vector (Only needed when using a dense retriever (e.g. DensePassageRetriever, EmbeddingRetriever) on top)
        :param embedding_dim: Dimensionality of embedding vector (Only needed when using a dense retriever (e.g. DensePassageRetriever, EmbeddingRetriever) on top)
        :param custom_mapping: If you want to use your own custom mapping for creating a new index in Elasticsearch, you can supply it here as a dictionary.
        :param excluded_meta_data: Name of fields in Elasticsearch that should not be returned (e.g. [field_one, field_two]).
                                   Helpful if you have fields with long, irrelevant content that you don't want to display in results (e.g. embedding vectors).
        :param scheme: 'https' or 'http', protocol used to connect to your elasticsearch instance
        :param ca_certs: Root certificates for SSL
        :param verify_certs: Whether to be strict about ca certificates
        :param create_index: Whether to try creating a new index (If the index of that name is already existing, we will just continue in any case)
        """
        self.client = Elasticsearch(hosts=[{"host": host, "port": port}], http_auth=(username, password),
                                    scheme=scheme, ca_certs=ca_certs, verify_certs=verify_certs)

        # configure mappings to ES fields that will be used for querying / displaying results
        if type(search_fields) == str:
            search_fields = [search_fields]

        #TODO we should implement a more flexible interal mapping here that simplifies the usage of additional,
        # custom fields (e.g. meta data you want to return)
        self.search_fields = search_fields
        self.text_field = text_field
        self.name_field = name_field
        self.embedding_field = embedding_field
        self.embedding_dim = embedding_dim
        self.excluded_meta_data = excluded_meta_data
        self.faq_question_field = faq_question_field

        self.custom_mapping = custom_mapping
        if create_index:
            self._create_document_index(index)
        self.index: str = index

        self._create_label_index(label_index)
        self.label_index = label_index

    def _create_document_index(self, index_name):
        if self.custom_mapping:
            mapping = self.custom_mapping
        else:
            mapping = {
                "mappings": {
                    "properties": {
                        self.name_field: {"type": "text"},
                        self.text_field: {"type": "text"},
                    }
                }
            }
            if self.embedding_field:
                mapping["mappings"]["properties"][self.embedding_field] = {"type": "dense_vector", "dims": self.embedding_dim}
        self.client.indices.create(index=index_name, ignore=400, body=mapping)

    def _create_label_index(self, index_name):
        mapping = {
            "mappings": {
                "properties": {
                    "question": {"type": "text"},
                    "answer": {"type": "text"},
                    "is_correct_answer": {"type": "boolean"},
                    "is_correct_document": {"type": "boolean"},
                    "origin": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "offset_start_in_doc": {"type": "long"},
                    "no_answer": {"type": "boolean"},
                    "model_id": {"type": "keyword"},
                    "type": {"type": "keyword"},
                }
            }
        }
        self.client.indices.create(index=index_name, ignore=400, body=mapping)

    def get_document_by_id(self, id: Union[UUID, str], index=None) -> Optional[Document]:
        if index is None:
            index = self.index
        query = {"query": {"ids": {"values": [id]}}}
        result = self.client.search(index=index, body=query)["hits"]["hits"]

        document = self._convert_es_hit_to_document(result[0]) if result else None
        return document

    def get_document_ids_by_tags(self, tags: dict, index: Optional[str]) -> List[str]:
        index = index or self.index
        term_queries = [{"terms": {key: value}} for key, value in tags.items()]
        query = {"query": {"bool": {"must": term_queries}}}
        logger.debug(f"Tag filter query: {query}")
        result = self.client.search(index=index, body=query, size=10000)["hits"]["hits"]
        doc_ids = []
        for hit in result:
            doc_ids.append(hit["_id"])
        return doc_ids

    def write_documents(self, documents: Union[List[dict], List[Document]], index: Optional[str] = None):
        """
        Indexes documents for later queries in Elasticsearch.

        :param documents: a list of Python dictionaries or a list of Haystack Document objects.
                          For documents as dictionaries, the format is {"text": "<the-actual-text>"}.
                          Optionally: Include meta data via {"text": "<the-actual-text>",
                          "meta":{"name": "<some-document-name>, "author": "somebody", ...}}
                          It can be used for filtering and is accessible in the responses of the Finder.
                          Advanced: If you are using your own Elasticsearch mapping, the key names in the dictionary
                          should be changed to what you have set for self.text_field and self.name_field.
        :param index: Elasticsearch index where the documents should be indexed. If not supplied, self.index will be used.
        :return: None
        """

        if index and not self.client.indices.exists(index=index):
            self._create_document_index(index)

        if index is None:
            index = self.index

        # Make sure we comply to Document class format
        documents_objects = [Document.from_dict(d) if isinstance(d, dict) else d for d in documents]

        documents_to_index = []
        for doc in documents_objects:

            _doc = {
                "_op_type": "create",
                "_index": index,
                **doc.to_dict()
            }  # type: Dict[str, Any]

            # rename id for elastic
            _doc["_id"] = str(_doc.pop("id"))

            # don't index query score and empty fields
            _ = _doc.pop("query_score", None)
            _doc = {k:v for k,v in _doc.items() if v is not None}

            # In order to have a flat structure in elastic + similar behaviour to the other DocumentStores,
            # we "unnest" all value within "meta"
            if "meta" in _doc.keys():
                for k, v in _doc["meta"].items():
                    _doc[k] = v
                _doc.pop("meta")
            documents_to_index.append(_doc)
        bulk(self.client, documents_to_index, request_timeout=300, refresh="wait_for")

    def write_labels(self, labels: Union[List[Label], List[dict]], index: Optional[str] = "label"):
        if index and not self.client.indices.exists(index=index):
            self._create_label_index(index)

        # Make sure we comply to Label class format
        label_objects = [Label.from_dict(l) if isinstance(l, dict) else l for l in labels]

        labels_to_index = []
        for label in label_objects:
            _label = {
                "_op_type": "create",
                "_index": index,
                **label.to_dict()
            }  # type: Dict[str, Any]

            labels_to_index.append(_label)
        bulk(self.client, labels_to_index, request_timeout=300, refresh="wait_for")

    def update_document_meta(self, id: str, meta: Dict[str, str]):
        body = {"doc": meta}
        self.client.update(index=self.index, doc_type="_doc", id=id, body=body, refresh="wait_for")

    def get_document_count(self, index: Optional[str] = None) -> int:
        if index is None:
            index = self.index
        result = self.client.count(index=index)
        count = result["count"]
        return count

    def get_label_count(self, index: Optional[str] = None) -> int:
        return self.get_document_count(index=index)

    def get_all_documents(self, index: Optional[str] = None, filters: Optional[dict] = None) -> List[Document]:
        if index is None:
            index = self.index

        result = self.get_all_documents_in_index(index=index, filters=filters)
        documents = [self._convert_es_hit_to_document(hit) for hit in result]

        return documents

    def get_all_labels(self, index: str = "label", filters: Optional[dict] = None) -> List[Label]:
        result = self.get_all_documents_in_index(index=index, filters=filters)
        labels = [Label.from_dict(hit["_source"]) for hit in result]
        return labels

    def get_all_documents_in_index(self, index: str, filters: Optional[dict] = None) -> List[dict]:
        body = {
            "query": {
                "bool": {
                    "must": {
                        "match_all": {}
                    }
                }
            }
        }  # type: Dict[str, Any]

        if filters:
            filter_clause = []
            for key, values in filters.items():
                filter_clause.append(
                    {
                        "terms": {key: values}
                    }
                )
            body["query"]["bool"]["filter"] = filter_clause
        result = scan(self.client, query=body, index=index)

        return result

    def query(
        self,
        query: Optional[str],
        filters: Optional[Dict[str, List[str]]] = None,
        top_k: int = 10,
        custom_query: Optional[str] = None,
        index: Optional[str] = None,
    ) -> List[Document]:

        if index is None:
            index = self.index

        # Naive retrieval without BM25, only filtering
        if query is None:
            body = {"query":
                        {"bool": {"must":
                                      {"match_all": {}}}}}  # type: Dict[str, Any]
            if filters:
                filter_clause = []
                for key, values in filters.items():
                    filter_clause.append(
                        {
                            "terms": {key: values}
                        }
                    )
                body["query"]["bool"]["filter"] = filter_clause

        # Retrieval via custom query
        elif custom_query:  # substitute placeholder for question and filters for the custom_query template string
            template = Template(custom_query)
            # replace all "${question}" placeholder(s) with query
            substitutions = {"question": query}
            # For each filter we got passed, we'll try to find & replace the corresponding placeholder in the template
            # Example: filters={"years":[2018]} => replaces {$years} in custom_query with '[2018]'
            if filters:
                for key, values in filters.items():
                    values_str = json.dumps(values)
                    substitutions[key] = values_str
            custom_query_json = template.substitute(**substitutions)
            body = json.loads(custom_query_json)
            # add top_k
            body["size"] = str(top_k)

        # Default Retrieval via BM25 using the user query on `self.search_fields`
        else:
            body = {
                "size": str(top_k),
                "query": {
                    "bool": {
                        "should": [{"multi_match": {"query": query, "type": "most_fields", "fields": self.search_fields}}]
                    }
                },
            }

            if filters:
                filter_clause = []
                for key, values in filters.items():
                    if type(values) != list:
                        raise ValueError(f'Wrong filter format for key "{key}": Please provide a list of allowed values for each key. '
                                         'Example: {"name": ["some", "more"], "category": ["only_one"]} ')
                    filter_clause.append(
                        {
                            "terms": {key: values}
                        }
                    )
                body["query"]["bool"]["filter"] = filter_clause

        if self.excluded_meta_data:
            body["_source"] = {"excludes": self.excluded_meta_data}

        logger.debug(f"Retriever query: {body}")
        result = self.client.search(index=index, body=body)["hits"]["hits"]

        documents = [self._convert_es_hit_to_document(hit) for hit in result]
        return documents

    def query_by_embedding(self,
                           query_emb: np.array,
                           filters: Optional[dict] = None,
                           top_k: int = 10,
                           index: Optional[str] = None) -> List[Document]:
        if index is None:
            index = self.index

        if not self.embedding_field:
            raise RuntimeError("Please specify arg `embedding_field` in ElasticsearchDocumentStore()")
        else:
            # +1 in cosine similarity to avoid negative numbers
            body= {
                "size": top_k,
                "query": {
                    "script_score": {
                        "query": {"match_all": {}},
                        "script": {
                            "source": f"cosineSimilarity(params.query_vector,doc['{self.embedding_field}']) + 1.0",
                            "params": {
                                "query_vector": query_emb.tolist()
                            }
                        }
                    }
                }
            }  # type: Dict[str,Any]

            if filters:
                filter_clause = []
                for key, values in filters.items():
                    filter_clause.append(
                        {
                            "terms": {key: values}
                        }
                    )
                body["query"]["bool"]["filter"] = filter_clause

            if self.excluded_meta_data:
                body["_source"] = {"excludes": self.excluded_meta_data}

            logger.debug(f"Retriever query: {body}")
            result = self.client.search(index=index, body=body, request_timeout=300)["hits"]["hits"]

            documents = [self._convert_es_hit_to_document(hit, score_adjustment=-1) for hit in result]
            return documents

    def _convert_es_hit_to_document(self, hit: dict, score_adjustment: int = 0) -> Document:
        # We put all additional data of the doc into meta_data and return it in the API
        meta_data = {k:v for k,v in hit["_source"].items() if k not in (self.text_field, self.faq_question_field, self.embedding_field, "tags")}
        meta_data["name"] = meta_data.pop(self.name_field, None)

        document = Document(
            id=hit["_id"],
            text=hit["_source"].get(self.text_field),
            meta=meta_data,
            query_score=hit["_score"] + score_adjustment if hit["_score"] else None,
            question=hit["_source"].get(self.faq_question_field),
            tags=hit["_source"].get("tags"),
            embedding=hit["_source"].get(self.embedding_field)
        )
        return document

    def describe_documents(self, index=None):
        if index is None:
            index = self.index
        docs = self.get_all_documents(index)

        l = [len(d.text) for d in docs]
        stats = {"count": len(docs),
                 "chars_mean": np.mean(l),
                 "chars_max": max(l),
                 "chars_min": min(l),
                 "chars_median": np.median(l),
                 }
        return stats

    def update_embeddings(self, retriever, index=None):
        """
        Updates the embeddings in the the document store using the encoding model specified in the retriever.
        This can be useful if want to add or change the embeddings for your documents (e.g. after changing the retriever config).

        :param retriever: Retriever
        :return: None
        """
        if index is None:
            index = self.index

        if not self.embedding_field:
            raise RuntimeError("Specify the arg `embedding_field` when initializing ElasticsearchDocumentStore()")

        docs = self.get_all_documents(index)
        passages = [d.text for d in docs]

        #TODO Index embeddings every X batches to avoid OOM for huge document collections
        logger.info(f"Updating embeddings for {len(passages)} docs ...")
        embeddings = retriever.embed_passages(passages)

        assert len(docs) == len(embeddings)

        if embeddings[0].shape[0] != self.embedding_dim:
            raise RuntimeError(f"Embedding dim. of model ({embeddings[0].shape[0]})"
                               f" doesn't match embedding dim. in documentstore ({self.embedding_dim})."
                               "Specify the arg `embedding_dim` when initializing ElasticsearchDocumentStore()")
        doc_updates = []
        for doc, emb in zip(docs, embeddings):
            update = {"_op_type": "update",
                      "_index": index,
                      "_id": doc.id,
                      "doc": {self.embedding_field: emb.tolist()},
                      }
            doc_updates.append(update)

        bulk(self.client, doc_updates, request_timeout=300)

    def add_eval_data(self, filename: str, doc_index: str = "eval_document", label_index: str = "label"):
        """
        Adds a SQuAD-formatted file to the DocumentStore in order to be able to perform evaluation on it.

        :param filename: Name of the file containing evaluation data
        :type filename: str
        :param doc_index: Elasticsearch index where evaluation documents should be stored
        :type doc_index: str
        :param label_index: Elasticsearch index where labeled questions should be stored
        :type label_index: str
        """

        docs, labels = eval_data_from_file(filename)
        self.write_documents(docs, index=doc_index)
        self.write_labels(labels, index=label_index)

    def delete_all_documents(self, index: str):
        """
        Delete all documents in a index.

        :param index: index name
        :return: None
        """
        self.client.delete_by_query(index=index, body={"query": {"match_all": {}}}, ignore=[404])
        # We want to be sure that all docs are deleted before continuing (delete_by_query doesn't support wait_for)
        time.sleep(1)






