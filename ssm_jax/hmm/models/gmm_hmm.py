from functools import partial

import chex
import jax.numpy as jnp
import jax.random as jr
import numpy as np
import tensorflow_probability.substrates.jax.bijectors as tfb
import tensorflow_probability.substrates.jax.distributions as tfd
from jax import tree_map
from jax import vmap
from jax.scipy.special import logsumexp
from jax.tree_util import register_pytree_node_class
from sklearn.cluster import KMeans
from sklearn.cluster import kmeans_plusplus
from ssm_jax.abstractions import Parameter
from ssm_jax.distributions import NormalInverseWishart
from ssm_jax.distributions import niw_posterior_update
from ssm_jax.hmm.inference import compute_transition_probs
from ssm_jax.hmm.inference import hmm_smoother
from ssm_jax.hmm.models.base import StandardHMM
from ssm_jax.utils import PSDToRealBijector


@chex.dataclass
class GMMHMMSuffStats:
    # Wrapper for sufficient statistics of a GMMHMM
    marginal_loglik: chex.Scalar
    initial_probs: chex.Array
    trans_probs: chex.Array
    N: chex.Array
    Sx: chex.Array
    SxxT: chex.Array


@register_pytree_node_class
class GaussianMixtureHMM(StandardHMM):
    """
    Hidden Markov Model with Gaussian mixture emissions.
    Attributes
    ----------
    weights : array, shape (num_states, num_emission_components)
        Mixture weights for each state.
    emission_means : array, shape (num_states, num_emission_components, emission_dim)
        Mean parameters for each mixture component in each state.
    emission_covariance_matrices : array
        Covariance parameters for each mixture components in each state.
    """

    def __init__(self,
                 initial_probabilities,
                 transition_matrix,
                 weights,
                 emission_means,
                 emission_covariance_matrices,
                 initial_probs_concentration=1.1,
                 transition_matrix_concentration=1.1,
                 emission_mixture_weights_concentration=1.1,
                 emission_prior_mean=0.,
                 emission_prior_mean_concentration=1e-4,
                 emission_prior_extra_df=1e-4,
                 emission_prior_covariance_matrices_scale=0.1):

        super().__init__(initial_probabilities,
                         transition_matrix,
                         initial_probs_concentration=initial_probs_concentration,
                         transition_matrix_concentration=transition_matrix_concentration)

        self._emission_mixture_weights = Parameter(weights, bijector=tfb.Invert(tfb.SoftmaxCentered()))
        self._emission_means = Parameter(emission_means)
        self._emission_covs = Parameter(emission_covariance_matrices, bijector=PSDToRealBijector)

        num_states, num_components, emission_dim = emission_means.shape

        _emission_mixture_weights_concentration = emission_mixture_weights_concentration * jnp.ones(
            (num_components,)) if isinstance(emission_mixture_weights_concentration,
                                             float) else emission_mixture_weights_concentration
        assert _emission_mixture_weights_concentration.shape == (num_components,)
        self._emission_mixture_weights_concentration = Parameter(_emission_mixture_weights_concentration,
                                                                 is_frozen=True,
                                                                 bijector=tfb.Invert(tfb.Softplus()))

        _emission_prior_mean = emission_prior_mean * jnp.ones(
            (num_components, emission_dim)) if isinstance(emission_prior_mean, float) else emission_prior_mean
        assert _emission_prior_mean.shape == (num_components, emission_dim)
        self._emission_prior_mean = Parameter(_emission_prior_mean, is_frozen=True)

        _emission_prior_mean_concentration = emission_prior_mean_concentration * jnp.ones(
            (num_components,)) if isinstance(emission_prior_mean_concentration,
                                             float) else emission_prior_mean_concentration
        assert _emission_prior_mean_concentration.shape == (num_components,)
        self._emission_prior_mean_concentration = Parameter(_emission_prior_mean_concentration, is_frozen=True)

        _emission_prior_covariance_matrices_df = emission_dim + emission_prior_extra_df * jnp.ones(
            (num_components,)) if isinstance(emission_prior_extra_df, float) else emission_dim + emission_prior_extra_df
        assert _emission_prior_covariance_matrices_df.shape == (num_components,)
        self._emission_prior_covariance_matrices_df = Parameter(_emission_prior_covariance_matrices_df, is_frozen=True)

        _emission_prior_covariance_matrices_scale = emission_prior_covariance_matrices_scale * jnp.ones(
            (num_components, emission_dim, emission_dim)) if isinstance(
                emission_prior_covariance_matrices_scale, float) else emission_prior_covariance_matrices_scale
        assert _emission_prior_covariance_matrices_scale.shape == (num_components, emission_dim, emission_dim)
        self._emission_prior_covariance_matrices_scale = Parameter(_emission_prior_covariance_matrices_scale,
                                                                   is_frozen=True)

    @classmethod
    def random_initialization(cls, key, num_states, num_components, emission_dim, emissions=None):
        key1, key2, key3, key4 = jr.split(key, 4)
        initial_probs = jr.dirichlet(key1, jnp.ones(num_states))
        transition_matrix = jr.dirichlet(key2, jnp.ones(num_states), (num_states,))
        emission_mixture_weights = jr.dirichlet(key3, jnp.ones(num_components), shape=(num_states,))
        if emissions is None:
            emission_means = jr.normal(key4, (num_states, num_components, emission_dim))
        else:
            
            main_kmeans = KMeans(n_clusters=num_states,
                                 random_state=42)
            
            covariance_matrix = None
            labels = main_kmeans.fit_predict(emissions)
            main_centroid = np.mean(main_kmeans.cluster_centers_, axis=0)
            emission_means = []
            for label in range(num_states):
                kmeans = KMeans(n_clusters=num_states, random_state=label)
                cluster = emissions[jnp.where(labels == label)]
                if cluster.shape[0] >= num_components:
                    kmeans.fit(np.array(cluster))
                    emission_means.append(jnp.array(kmeans.cluster_centers_))
                else:
                    if covariance_matrix is None:
                        covariance_matrix = jnp.cov(emissions.T) + 1e-6 * jnp.eye(emission_dim)
                    key4, key = jr.split(key)
                    m_cluster = jr.multivariate_normal(key4, main_centroid, cov=covariance_matrix, size=num_components)
                    emission_means.append(m_cluster)
            emission_means = jnp.vstack(emission_means)
        emission_covs = jnp.eye(emission_dim) * jnp.ones((num_states, num_components, emission_dim, emission_dim))
        return cls(initial_probs, transition_matrix, emission_mixture_weights, emission_means, emission_covs)

    # Properties to get various parameters of the model

    @property
    def emission_mixture_weights(self):
        return self._emission_mixture_weights

    @property
    def emission_means(self):
        return self._emission_means

    @property
    def emission_covariance_matrices(self):
        return self._emission_covs

    def emission_distribution(self, state):
        return tfd.MixtureSameFamily(
            mixture_distribution=tfd.Categorical(probs=self._emission_mixture_weights.value[state]),
            components_distribution=tfd.MultivariateNormalFullCovariance(
                loc=self._emission_means.value[state], covariance_matrix=self._emission_covs.value[state]))

    def log_prior(self):
        lp = tfd.Dirichlet(self._initial_probs_concentration.value).log_prob(self.initial_probs.value)
        lp += tfd.Dirichlet(self._transition_matrix_concentration.value).log_prob(self.transition_matrix.value).sum()
        lp += tfd.Dirichlet(self._emission_mixture_weights_concentration.value).log_prob(
            self.emission_mixture_weights.value).sum()
        return lp

    # Expectation-maximization (EM) code

    def e_step(self, batch_emissions):

        def _single_e_step(emissions):
            # Run the smoother
            posterior = hmm_smoother(self._compute_initial_probs(), self._compute_transition_matrices(),
                                     self._compute_conditional_logliks(emissions))

            # Compute the initial state and transition probabilities
            initial_probs = posterior.smoothed_probs[0]
            trans_probs = compute_transition_probs(self.transition_matrix.value, posterior)

            def prob_fn(x):
                logprobs = vmap(lambda mus, sigmas, weights: tfd.MultivariateNormalFullCovariance(
                    loc=mus, covariance_matrix=sigmas).log_prob(x) + jnp.log(weights))(
                        self._emission_means.value, self._emission_covs.value, self._emission_mixture_weights.value)
                logprobs = logprobs - logsumexp(logprobs, axis=-1, keepdims=True)

                return jnp.exp(logprobs)

            prob_denses = vmap(prob_fn)(emissions)
            N = jnp.einsum("tk,tkm->tkm", posterior.smoothed_probs, prob_denses)
            Sx = jnp.einsum("tkm,tn->kmn", N, emissions)
            SxxT = jnp.einsum("tkm,tn,tl->kmnl", N, emissions, emissions)
            N = N.sum(axis=0)

            stats = GMMHMMSuffStats(marginal_loglik=posterior.marginal_loglik,
                                    initial_probs=initial_probs,
                                    trans_probs=trans_probs,
                                    N=N,
                                    Sx=Sx,
                                    SxxT=SxxT)
            return stats

        # Map the E step calculations over batches
        return vmap(_single_e_step)(batch_emissions)

    def _m_step_emissions(self, batch_emissions, batch_posteriors, **kwargs):
        # Sum the statistics across all batches

        stats = tree_map(partial(jnp.sum, axis=0), batch_posteriors)

        def _single_m_step(Sx, SxxT, N):

            def posterior_mode(loc, mean_concentration, df, scale, *stats):
                niw_prior = NormalInverseWishart(loc=loc, mean_concentration=mean_concentration, df=df, scale=scale)
                return niw_posterior_update(niw_prior, stats).mode()

            nu_post = self._emission_mixture_weights_concentration.value + N
            return tfd.Dirichlet(nu_post).mode(), *vmap(posterior_mode)(self._emission_prior_mean.value, self._emission_prior_mean_concentration.value, self._emission_prior_covariance_matrices_df.value, self._emission_prior_covariance_matrices_scale.value, Sx, SxxT, N)

        emission_mixture_weights, covariance_matrices, means = vmap(_single_m_step)(stats.Sx, stats.SxxT, stats.N)
        self._emission_mixture_weights.value = emission_mixture_weights
        self._emission_covs.value = covariance_matrices
        self._emission_means.value = means
