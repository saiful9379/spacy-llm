from collections import defaultdict
from itertools import tee
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple, Union, cast

import spacy
from spacy import util
from spacy.language import Language
from spacy.pipeline import Pipe
from spacy.tokens import Doc
from spacy.training import Example
from spacy.ty import InitializableComponent as Initializable
from spacy.vocab import Vocab

from .. import registry  # noqa: F401
from ..compat import TypedDict
from ..ty import Cache, LLMTask, PromptExecutor, Scorable, Serializable, validate_types


class CacheConfigType(TypedDict):
    path: Optional[Path]
    batch_size: int
    max_batches_in_mem: int


@Language.factory(
    "llm",
    requires=[],
    assigns=[],
    default_config={
        "task": None,
        "backend": {
            "@llm_backends": "spacy.REST.v1",
            "api": "OpenAI",
            "config": {"model": "gpt-3.5-turbo"},
            "strict": True,
        },
        "cache": {
            "@llm_misc": "spacy.BatchCache.v1",
            "path": None,
            "batch_size": 64,
            "max_batches_in_mem": 4,
        },
        "save_io": False,
    },
)
def make_llm(
    nlp: Language,
    name: str,
    task: Optional[LLMTask],
    backend: PromptExecutor,
    cache: Cache,
    save_io: bool,
) -> "LLMWrapper":
    """Construct an LLM component.

    nlp (Language): Pipeline.
    name (str): The component instance name, used to add entries to the
        losses during training.
    task (Optional[LLMTask]): An LLMTask can generate prompts for given docs, and can parse the LLM's responses into
        structured information and set that back on the docs.
    backend (Callable[[Iterable[Any]], Iterable[Any]]]): Callable querying the specified LLM API.
    cache (Cache): Cache to use for caching prompts and responses per doc (batch).
    save_io (bool): Whether to save LLM I/O (prompts and responses) in the `Doc._.llm_io` custom extension.
    """
    if task is None:
        raise ValueError(
            "Argument `task` has not been specified, but is required (e. g. {'@llm_tasks': "
            "'spacy.NER.v2'})."
        )
    validate_types(task, backend)

    return LLMWrapper(
        name=name,
        task=task,
        save_io=save_io,
        backend=backend,
        cache=cache,
        vocab=nlp.vocab,
    )


class LLMWrapper(Pipe):
    """Pipeline component for wrapping LLMs."""

    def __init__(
        self,
        name: str = "LLMWrapper",
        *,
        vocab: Vocab,
        task: LLMTask,
        backend: PromptExecutor,
        cache: Cache,
        save_io: bool,
    ) -> None:
        """
        Component managing execution of prompts to LLM APIs and mapping responses back to Doc/Span instances.

        name (str): The component instance name, used to add entries to the
            losses during training.
        vocab (Vocab): Pipeline vocabulary.
        task (Optional[LLMTask]): An LLMTask can generate prompts for given docs, and can parse the LLM's responses into
            structured information and set that back on the docs.
        backend (Callable[[Iterable[Any]], Iterable[Any]]]): Callable querying the specified LLM API.
        cache (Cache): Cache to use for caching prompts and responses per doc (batch).
        save_io (bool): Whether to save LLM I/O (prompts and responses) in the `Doc._.llm_io` custom extension.
        """
        self._name = name
        self._task = task
        self._backend = backend
        self._cache = cache
        self._cache.vocab = vocab
        self._save_io = save_io

        # This is done this way because spaCy's `validate_init_settings` function
        # does not support `**kwargs: Any`.
        # See https://github.com/explosion/spaCy/blob/master/spacy/schemas.py#L111
        if isinstance(self._task, Initializable):
            self.initialize = self._task.initialize

    def __call__(self, doc: Doc) -> Doc:
        """Apply the LLM wrapper to a Doc instance.

        doc (Doc): The Doc instance to process.
        RETURNS (Doc): The processed Doc.
        """
        docs = self._process_docs([doc])
        assert isinstance(docs[0], Doc)
        return docs[0]

    def score(
        self, examples: Iterable[Example], **kwargs
    ) -> Dict[str, Union[float, Dict[str, float]]]:
        """Score a batch of examples.

        examples (Iterable[Example]): The examples to score.
        RETURNS (Dict[str, Any]): The scores.

        DOCS: https://spacy.io/api/pipe#score
        """
        if isinstance(self._task, Scorable):
            return self._task.scorer(examples)
        return {}

    def pipe(self, stream: Iterable[Doc], *, batch_size: int = 128) -> Iterator[Doc]:
        """Apply the LLM prompt to a stream of documents.

        stream (Iterable[Doc]): A stream of documents.
        batch_size (int): The number of documents to buffer.
        YIELDS (Doc): Processed documents in order.
        """
        error_handler = self.get_error_handler()
        for doc_batch in spacy.util.minibatch(stream, batch_size):
            try:
                yield from iter(self._process_docs(doc_batch))
            except Exception as e:
                error_handler(self._name, self, doc_batch, e)

    def _process_docs(self, docs: List[Doc]) -> List[Doc]:
        """Process a batch of docs with the configured LLM backend and task.
        If a cache is configured, only sends prompts to backend for docs not found in cache.

        docs (List[Doc]): Input batch of docs
        RETURNS (List[Doc]): Processed batch of docs with task annotations set
        """

        is_cached = [doc in self._cache for doc in docs]
        noncached_doc_batch = [doc for i, doc in enumerate(docs) if not is_cached[i]]
        modified_docs = iter(())
        if noncached_doc_batch:
            prompts = self._task.generate_prompts(noncached_doc_batch)
            if self._save_io:
                prompts, saved_prompts = tee(prompts)

            responses = self._backend(prompts)
            if self._save_io:
                responses, saved_responses = tee(responses)

            modified_docs = iter(
                self._task.parse_responses(noncached_doc_batch, responses)
            )

        final_docs = []

        for i, doc in enumerate(docs):
            if is_cached[i]:
                cached_doc = self._cache[doc]
                assert cached_doc is not None
                final_docs.append(cached_doc)
            else:
                doc = next(modified_docs)
                self._cache.add(doc)
                final_docs.append(doc)

                if self._save_io:
                    # Make sure the `llm_io` field is set
                    doc.user_data["llm_io"] = doc.user_data.get(
                        "llm_io", defaultdict(dict)
                    )
                    llm_io = doc.user_data["llm_io"][self._name]
                    llm_io["prompt"] = str(next(saved_prompts))
                    llm_io["response"] = str(next(saved_responses))

        return final_docs

    def to_bytes(
        self,
        *,
        exclude: Tuple[str] = cast(Tuple[str], tuple()),
    ) -> bytes:
        """Serialize the LLMWrapper to a bytestring.

        exclude (Tuple): Names of properties to exclude from serialization.
        RETURNS (bytes): The serialized object.
        """

        serialize = {}

        if isinstance(self._task, Serializable):
            serialize["task"] = lambda: self._task.to_bytes(exclude=exclude)  # type: ignore[attr-defined]
        if isinstance(self._backend, Serializable):
            serialize["backend"] = lambda: self._backend.to_bytes(exclude=exclude)  # type: ignore[attr-defined]

        return util.to_bytes(serialize, exclude)

    def from_bytes(
        self,
        bytes_data: bytes,
        *,
        exclude: Tuple[str] = cast(Tuple[str], tuple()),
    ) -> "LLMWrapper":
        """Load the LLMWrapper from a bytestring.

        bytes_data (bytes): The data to load.
        exclude (Tuple[str]): Names of properties to exclude from deserialization.
        RETURNS (LLMWrapper): Modified LLMWrapper instance.
        """

        deserialize = {}

        if isinstance(self._task, Serializable):
            deserialize["task"] = lambda b: self._task.from_bytes(b, exclude=exclude)  # type: ignore[attr-defined]
        if isinstance(self._backend, Serializable):
            deserialize["backend"] = lambda b: self._backend.from_bytes(b, exclude=exclude)  # type: ignore[attr-defined]

        util.from_bytes(bytes_data, deserialize, exclude)
        return self

    def to_disk(
        self, path: Path, *, exclude: Tuple[str] = cast(Tuple[str], tuple())
    ) -> None:
        """Serialize the LLMWrapper to disk.
        path (Path): A path (currently unused).
        exclude (Tuple): Names of properties to exclude from serialization.
        """

        serialize = {}

        if isinstance(self._task, Serializable):
            serialize["task"] = lambda p: self._task.to_disk(p, exclude=exclude)  # type: ignore[attr-defined]
        if isinstance(self._backend, Serializable):
            serialize["backend"] = lambda p: self._backend.to_disk(p, exclude=exclude)  # type: ignore[attr-defined]

        return util.to_disk(path, serialize, exclude)

    def from_disk(
        self, path: Path, *, exclude: Tuple[str] = cast(Tuple[str], tuple())
    ) -> "LLMWrapper":
        """Load the LLMWrapper from disk.
        path (Path): A path (currently unused).
        exclude (Tuple): Names of properties to exclude from deserialization.
        RETURNS (LLMWrapper): Modified LLMWrapper instance.
        """

        serialize = {}

        if isinstance(self._task, Serializable):
            serialize["task"] = lambda p: self._task.from_disk(p, exclude=exclude)  # type: ignore[attr-defined]
        if isinstance(self._backend, Serializable):
            serialize["backend"] = lambda p: self._backend.from_disk(p, exclude=exclude)  # type: ignore[attr-defined]

        util.from_disk(path, serialize, exclude)
        return self
