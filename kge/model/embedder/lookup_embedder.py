from torch import Tensor
import torch.nn
import torch.nn.functional
import numpy as np

from kge import Config, Dataset
from kge.job import Job
from kge.model import KgeEmbedder
from kge.misc import round_to_points

from typing import List, Dict


class LookupEmbedder(KgeEmbedder):
    def __init__(
        self, config: Config, dataset: Dataset, configuration_key: str, vocab_size: int
    ):
        super().__init__(config, dataset, configuration_key)

        # read config
        self.normalize_p = self.get_option("normalize.p")
        self.normalize_with_grad = self.get_option("normalize.with_grad")
        self.regularize = self.check_option("regularize", ["", "lp"])
        self.sparse = self.get_option("sparse")
        self.config.check("train.trace_level", ["batch", "epoch"])
        self.vocab_size = vocab_size

        round_embedder_dim_to = self.get_option("round_dim_to")
        if len(round_embedder_dim_to) > 0:
            self.dim = round_to_points(round_embedder_dim_to, self.dim)

        # setup embedder
        self._embeddings = torch.nn.Embedding(
            self.vocab_size, self.dim, sparse=self.sparse
        )

        # initialize weights
        init_ = self.get_option("initialize")
        try:
            init_args = self.get_option("initialize_args." + init_)
        except KeyError:
            init_args = self.get_option("initialize_args")

        # Automatically set arg a (lower bound) for uniform_ if not given
        if init_ == "uniform_" and "a" not in init_args:
            init_args["a"] = init_args["b"] * -1
            self.set_option("initialize_args.a", init_args["a"], log=True)

        self.initialize(self._embeddings.weight.data, init_, init_args)

        # TODO handling negative dropout because using it with ax searches for now
        dropout = self.get_option("dropout")
        if dropout < 0:
            if config.get("train.auto_correct"):
                config.log(
                    "Setting {}.dropout to 0, "
                    "was set to {}.".format(configuration_key, dropout)
                )
                dropout = 0
        self.dropout = torch.nn.Dropout(dropout)

    def prepare_job(self, job: Job, **kwargs):
        super().prepare_job(job, **kwargs)
        if self.normalize_p > 0:

            def normalize_embeddings(job):
                if self.normalize_with_grad:
                    self._embeddings.weight = torch.nn.functional.normalize(
                        self._embeddings.weight, p=self.normalize_p, dim=-1
                    )
                else:
                    with torch.no_grad():
                        self._embeddings.weight = torch.nn.Parameter(
                            torch.nn.functional.normalize(
                                self._embeddings.weight, p=self.normalize_p, dim=-1
                            )
                        )

            job.pre_batch_hooks.append(normalize_embeddings)

    @torch.no_grad()
    def init_pretrained(
        self,
        pretrained_embedder: torch.nn.Embedding,
        ids_pretrained_model: List,
        ids_new_model: List,
        ensure_all: bool = False,
    ) -> None:
        """
        Initialize embedding layer with pre-trained embeddings from another embedding
        layer.
        Maps embeddings based on the entity/relation ids provided
        Args:
            pretrained_embedder: Embedding layer with pre-trained embeddings
            ids_pretrained_model: entity/relation ids corresponding to the embeddings
                                  of the pre-trained embedding layer
            ids_new_model: entity/relation ids corresponding to the embeddings of the
                           model to be initialized
            ensure_all: ensure that all entities/relations can be initialized with
                        embeddings provided in the pre-trained model

        Returns:
            None
        """
        _, dataset_intersect_ind, checkpoint_intersect_ind = np.intersect1d(
            ids_new_model, ids_pretrained_model, return_indices=True
        )
        if ensure_all and not len(dataset_intersect_ind) == len(ids_new_model):
            raise IndexError(
                "Not all embeddings could be initialized with the embeddings provided "
                "in the pre-trained model"
            )
        self._embeddings.weight[
            torch.from_numpy(dataset_intersect_ind)
            .to(self._embeddings.weight.device)
            .long()
        ] = pretrained_embedder.weight[
            torch.from_numpy(checkpoint_intersect_ind).long()
        ].to(
            self._embeddings.weight.device
        )

    def embed(self, indexes: Tensor) -> Tensor:
        return self._postprocess(self._embeddings(indexes.long()))

    def embed_all(self) -> Tensor:
        return self._postprocess(self._embeddings_all())

    def _postprocess(self, embeddings: Tensor) -> Tensor:
        if self.dropout.p > 0:
            embeddings = self.dropout(embeddings)
        return embeddings

    def _embeddings_all(self) -> Tensor:
        return self._embeddings(
            torch.arange(
                self.vocab_size, dtype=torch.long, device=self._embeddings.weight.device
            )
        )

    def _get_regularize_weight(self) -> Tensor:
        return self.get_option("regularize_weight")

    def penalty(self, **kwargs) -> List[Tensor]:
        # TODO factor out to a utility method
        result = super().penalty(**kwargs)
        if self.regularize == "" or self.get_option("regularize_weight") == 0.0:
            pass
        elif self.regularize == "lp":
            p = (
                self.get_option("regularize_args.p")
                if self.has_option("regularize_args.p")
                else 2
            )
            regularize_weight = self._get_regularize_weight()
            if not self.get_option("regularize_args.weighted"):
                # unweighted Lp regularization
                parameters = self._embeddings_all()
                result += [
                    (
                        f"{self.configuration_key}.L{p}_penalty",
                        (regularize_weight / p * parameters.norm(p=p) ** p).sum(),
                    )
                ]
            else:
                # weighted Lp regularization
                unique_indexes, counts = torch.unique(
                    kwargs["indexes"], return_counts=True
                )
                parameters = self._embeddings(unique_indexes)
                if p % 2 == 1:
                    parameters = torch.abs(parameters)
                result += [
                    (
                        f"{self.configuration_key}.L{p}_penalty",
                        (
                            regularize_weight
                            / p
                            * (parameters ** p * counts.float().view(-1, 1))
                        ).sum()
                        # In contrast to unweighted Lp regularization, rescaling by
                        # number of triples/indexes is necessary here so that penalty
                        # term is correct in expectation
                        / len(kwargs["indexes"]),
                    )
                ]
        else:  # unknown regularization
            raise ValueError(f"Invalid value regularize={self.regularize}")

        return result
