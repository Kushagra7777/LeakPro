"""Geiping, Jonas, et al. "Inverting gradients-how easy is it to break privacy in federated learning?."."""
from collections.abc import Generator
from copy import deepcopy
from dataclasses import dataclass, field

import optuna
import torch
from torch import Tensor
from torch.nn import CrossEntropyLoss, Module
from torch.utils.data import DataLoader

from leakpro.attacks.gia_attacks.abstract_gia import AbstractGIA
from leakpro.fl_utils.data_utils import GiaImageClassifictaionExtension
from leakpro.fl_utils.gia_optimizers import MetaSGD
from leakpro.fl_utils.similarity_measurements import cosine_similarity_weights, total_variation, l2_distance
from leakpro.metrics.attack_result import GIAResults
from leakpro.utils.import_helper import Callable, Self
from leakpro.utils.logger import logger
from leakpro.fl_utils.data_utils import get_used_tokens


@dataclass
class InvertingConfig:
    """Possible configs for the Inverting Gradients attack."""

    # total variation scale for smoothing the reconstructions after each iteration
    tv_reg: float = 1.0e-06
    # learning rate on the attack optimizer
    attack_lr: float = 0.1
    # iterations for the attack steps
    at_iterations: int = 8000
    # MetaOptimizer, see MetaSGD for implementation
    optimizer: object = field(default_factory=lambda: MetaSGD())
    # Client loss function
    criterion: object = field(default_factory=lambda: CrossEntropyLoss())
    # Data modality extension
    data_extension: object = field(default_factory=lambda: GiaImageClassifictaionExtension())
    # Number of epochs for the client attack
    epochs: int = 1
    # if to use median pool 2d on images, can improve attack on high higher resolution (100+)
    median_pooling: bool = False
    # if we compare difference only for top 10 layers with largest changes. Potentially good for larger models.
    top10norms: bool = False
    # boolean if using image data or not (temporary solution)
    image_data: bool = True

class InvertingGradients(AbstractGIA):
    """Gradient inversion attack by Geiping et al."""

    def __init__(self: Self, model: Module, client_loader: DataLoader, train_fn: Callable,
                 data_mean: Tensor, data_std: Tensor, configs: InvertingConfig) -> None:
        super().__init__()
        self.original_model = model
        self.model = deepcopy(self.original_model)
        self.client_loader = client_loader
        self.train_fn = train_fn
        self.data_mean = data_mean
        self.data_std = data_std
        self.configs = configs
        self.best_loss = float("inf")
        self.best_reconstruction = None
        self.best_reconstruction_round = None
        logger.info("Inverting gradient initialized.")
        self.prepare_attack()
        
    def description(self:Self) -> dict:
        """Return a description of the attack."""
        title_str = "Inverting gradients"
        reference_str = """Geiping, Jonas, et al. Inverting gradients-how easy is it to
            break privacy in federated learning? Neurips, 2020."""
        summary_str = ""
        detailed_str = ""
        return {
            "title_str": title_str,
            "reference": reference_str,
            "summary": summary_str,
            "detailed": detailed_str,
        }

    def prepare_attack(self:Self) -> None:
        """Prepare the attack.

        Args:
        ----
            self (Self): The instance of the class.

        Returns:
        -------
            None

        """
        self.model.eval()
        client_gradient = self.train_fn(self.model, self.client_loader, self.configs.optimizer,
                                        self.configs.criterion, self.configs.epochs)
        self.client_gradient = [p.detach() for p in client_gradient]

        used_tokens = get_used_tokens(self.model, self.client_gradient)
        self.reconstruction, self.reconstruction_loader = self.configs.data_extension.get_at_data(self.client_loader, used_tokens)
        
        if isinstance(self.reconstruction, list):
            for r in self.reconstruction:
                r.requieres_grad = True
        else:
            self.reconstruction.requires_grad = True
        

    def run_attack(self:Self) -> Generator[tuple[int, Tensor, GIAResults]]:
        """Run the attack and return the combined metric result.

        Returns
        -------
            GIAResults: Container for results on GIA attacks.

        """
        if self.configs.image_data:
            grad_closure = self.gradient_closure
        else:   
            grad_closure = self.gradient_closure_text
            
        return self.generic_attack_loop(self.configs, grad_closure, self.configs.at_iterations, self.reconstruction,
                                self.data_mean, self.data_std, self.configs.attack_lr, self.configs.median_pooling,
                                self.client_loader, self.reconstruction_loader,self.configs.image_data)


    def gradient_closure(self: Self, optimizer: torch.optim.Optimizer) -> Callable:
        """Returns a closure function that performs a gradient descent step.

        The closure function computes the gradients, calculates the reconstruction loss,
        adds a total variation regularization term, and then performs backpropagation.
        """
        def closure() -> torch.Tensor:
            """Computes the reconstruction loss and performs backpropagation.

            This function zeroes out the gradients of the optimizer and the model,
            computes the gradient and reconstruction loss, logs the reconstruction loss,
            optionally adds a total variation term, performs backpropagation, and optionally
            modifies the gradient of the input image.

            Returns
            -------
                torch.Tensor: The reconstruction loss.

            """
            optimizer.zero_grad()
            self.model.zero_grad()

            gradient = self.train_fn(self.model, self.reconstruction_loader, self.configs.optimizer,
                                     self.configs.criterion, self.configs.epochs)
            rec_loss = cosine_similarity_weights(gradient, self.client_gradient, self.configs.top10norms)

            # Add the TV loss term to penalize large variations between pixels, encouraging smoother images.
            rec_loss += (self.configs.tv_reg * total_variation(self.reconstruction))
            rec_loss.backward()
            self.reconstruction.grad.sign_()
            return rec_loss
        return closure
    # TODO: A better solution to handle different gradient closure functions
    def gradient_closure_text(self: Self, optimizer: torch.optim.Optimizer) -> Callable:
        """Returns a closure function that performs a gradient descent step.

        The closure function computes the gradients, calculates the reconstruction loss,
        adds a total variation regularization term, and then performs backpropagation.
        """
        def closure() -> torch.Tensor:
            """Computes the reconstruction loss and performs backpropagation.

            This function zeroes out the gradients of the optimizer and the model,
            computes the gradient and reconstruction loss, logs the reconstruction loss,
            optionally adds a total variation term, performs backpropagation, and optionally
            modifies the gradient of the input image.

            Returns
            -------
                torch.Tensor: The reconstruction loss.

            """
            optimizer.zero_grad()
            self.model.zero_grad()

            gradient = self.train_fn(self.model, self.reconstruction_loader, self.configs.optimizer,
                                     self.configs.criterion, self.configs.epochs)
            rec_loss = l2_distance(gradient, self.client_gradient)

            # Add Penalties for negative data points
            for r in self.reconstruction:
                rec_loss += 0.01 * torch.sum(torch.relu(-r) ** 2)

            rec_loss.backward()
            #self.reconstruction.grad.sign_()
            return rec_loss
        return closure

    def _configure_attack(self: Self, configs: dict) -> None:
        pass

    def suggest_parameters(self: Self, trial: optuna.trial.Trial) -> None:
        """Suggest parameters to chose and range for optimization for the Inverting Gradient attack."""
        total_variation = trial.suggest_float("total_variation", 1e-6, 1e-1, log=True)
        self.configs.tv_reg = total_variation

    def reset_attack(self: Self) -> None:
        """Reset attack to initial state."""
        self.best_loss = float("inf")
        self.best_reconstruction = None
        self.best_reconstruction_round = None
        self.model = deepcopy(self.original_model)
        self.prepare_attack()
        logger.info("Inverting attack reset to initial state.")

    def get_configs(self: Self) -> dict:
        """Return configs used for attack."""
        return self.configs
    
    
