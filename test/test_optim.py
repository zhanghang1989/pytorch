import math
import unittest
import functools
from copy import deepcopy
import torch
import torch.optim as optim
import torch.legacy.optim as old_optim
import torch.nn.functional as F
from torch.optim import SGD
from torch.autograd import Variable
from torch import sparse
from torch.optim.lr_scheduler import LambdaLR, StepLR, MultiStepLR, ExponentialLR, CosineAnnealingLR, ReduceLROnPlateau
from common import TestCase, run_tests


def rosenbrock(tensor):
    x, y = tensor
    return (1 - x) ** 2 + 100 * (y - x ** 2) ** 2


def drosenbrock(tensor):
    x, y = tensor
    return torch.DoubleTensor((-400 * x * (y - x ** 2) - 2 * (1 - x), 200 * (y - x ** 2)))


def wrap_old_fn(old_fn, **config):
    def wrapper(closure, params, state):
        return old_fn(closure, params, config, state)
    return wrapper


class TestOptim(TestCase):

    def _test_rosenbrock(self, constructor, old_fn):
        params_t = torch.Tensor([1.5, 1.5])
        state = {}

        params = Variable(torch.Tensor([1.5, 1.5]), requires_grad=True)
        optimizer = constructor([params])

        solution = torch.Tensor([1, 1])
        initial_dist = params.data.dist(solution)

        def eval():
            optimizer.zero_grad()
            loss = rosenbrock(params)
            loss.backward()
            # loss.backward() will give **slightly** different
            # gradients, than drosenbtock, because of a different ordering
            # of floating point operations. In most cases it doesn't matter,
            # but some optimizers are so sensitive that they can temporarily
            # diverge up to 1e-4, just to converge again. This makes the
            # comparison more stable.
            params.grad.data.copy_(drosenbrock(params.data))
            return loss

        for i in range(2000):
            optimizer.step(eval)
            old_fn(lambda _: (rosenbrock(params_t), drosenbrock(params_t)),
                   params_t, state)
            self.assertEqual(params.data, params_t)

        self.assertLessEqual(params.data.dist(solution), initial_dist)

    def _test_rosenbrock_sparse(self, constructor, sparse_only=False):
        params_t = torch.Tensor([1.5, 1.5])

        params = Variable(params_t, requires_grad=True)
        optimizer = constructor([params])

        if not sparse_only:
            params_c = Variable(params_t.clone(), requires_grad=True)
            optimizer_c = constructor([params_c])

        solution = torch.Tensor([1, 1])
        initial_dist = params.data.dist(solution)

        def eval(params, sparse_grad, w):
            # Depending on w, provide only the x or y gradient
            optimizer.zero_grad()
            loss = rosenbrock(params)
            loss.backward()
            grad = drosenbrock(params.data)
            # NB: We torture test the optimizer by returning an
            # uncoalesced sparse tensor
            if w:
                i = torch.LongTensor([[0, 0]])
                x = grad[0]
                v = torch.DoubleTensor([x / 4., x - x / 4.])
            else:
                i = torch.LongTensor([[1, 1]])
                y = grad[1]
                v = torch.DoubleTensor([y - y / 4., y / 4.])
            x = sparse.DoubleTensor(i, v, torch.Size([2]))
            if sparse_grad:
                params.grad.data = x
            else:
                params.grad.data = x.to_dense()
            return loss

        for i in range(2000):
            # Do cyclic coordinate descent
            w = i % 2
            optimizer.step(functools.partial(eval, params, True, w))
            if not sparse_only:
                optimizer_c.step(functools.partial(eval, params_c, False, w))
                self.assertEqual(params.data, params_c.data)

        self.assertLessEqual(params.data.dist(solution), initial_dist)

    def _test_basic_cases_template(self, weight, bias, input, constructor):
        weight = Variable(weight, requires_grad=True)
        bias = Variable(bias, requires_grad=True)
        input = Variable(input)
        optimizer = constructor(weight, bias)

        # to check if the optimizer can be printed as a string
        optimizer.__repr__()

        def fn():
            optimizer.zero_grad()
            y = weight.mv(input)
            if y.is_cuda and bias.is_cuda and y.get_device() != bias.get_device():
                y = y.cuda(bias.get_device())
            loss = (y + bias).pow(2).sum()
            loss.backward()
            return loss

        initial_value = fn().item()
        for i in range(200):
            optimizer.step(fn)
        self.assertLess(fn().item(), initial_value)

    def _test_state_dict(self, weight, bias, input, constructor):
        weight = Variable(weight, requires_grad=True)
        bias = Variable(bias, requires_grad=True)
        input = Variable(input)

        def fn_base(optimizer, weight, bias):
            optimizer.zero_grad()
            i = input_cuda if weight.is_cuda else input
            loss = (weight.mv(i) + bias).pow(2).sum()
            loss.backward()
            return loss

        optimizer = constructor(weight, bias)
        fn = functools.partial(fn_base, optimizer, weight, bias)

        # Prime the optimizer
        for i in range(20):
            optimizer.step(fn)
        # Clone the weights and construct new optimizer for them
        weight_c = Variable(weight.data.clone(), requires_grad=True)
        bias_c = Variable(bias.data.clone(), requires_grad=True)
        optimizer_c = constructor(weight_c, bias_c)
        fn_c = functools.partial(fn_base, optimizer_c, weight_c, bias_c)
        # Load state dict
        state_dict = deepcopy(optimizer.state_dict())
        state_dict_c = deepcopy(optimizer.state_dict())
        optimizer_c.load_state_dict(state_dict_c)
        # Run both optimizations in parallel
        for i in range(20):
            optimizer.step(fn)
            optimizer_c.step(fn_c)
            self.assertEqual(weight, weight_c)
            self.assertEqual(bias, bias_c)
        # Make sure state dict wasn't modified
        self.assertEqual(state_dict, state_dict_c)

        # Check that state dict can be loaded even when we cast parameters
        # to a different type and move to a different device.
        if not torch.cuda.is_available():
            return

        input_cuda = Variable(input.data.float().cuda())
        weight_cuda = Variable(weight.data.float().cuda(), requires_grad=True)
        bias_cuda = Variable(bias.data.float().cuda(), requires_grad=True)
        optimizer_cuda = constructor(weight_cuda, bias_cuda)
        fn_cuda = functools.partial(fn_base, optimizer_cuda, weight_cuda, bias_cuda)

        state_dict = deepcopy(optimizer.state_dict())
        state_dict_c = deepcopy(optimizer.state_dict())
        optimizer_cuda.load_state_dict(state_dict_c)

        # Make sure state dict wasn't modified
        self.assertEqual(state_dict, state_dict_c)

        for i in range(20):
            optimizer.step(fn)
            optimizer_cuda.step(fn_cuda)
            self.assertEqual(weight, weight_cuda)
            self.assertEqual(bias, bias_cuda)

    def _test_basic_cases(self, constructor, ignore_multidevice=False):
        self._test_state_dict(
            torch.randn(10, 5),
            torch.randn(10),
            torch.randn(5),
            constructor
        )
        self._test_basic_cases_template(
            torch.randn(10, 5),
            torch.randn(10),
            torch.randn(5),
            constructor
        )
        # non-contiguous parameters
        self._test_basic_cases_template(
            torch.randn(10, 5, 2)[..., 0],
            torch.randn(10, 2)[..., 0],
            torch.randn(5),
            constructor
        )
        # CUDA
        if not torch.cuda.is_available():
            return
        self._test_basic_cases_template(
            torch.randn(10, 5).cuda(),
            torch.randn(10).cuda(),
            torch.randn(5).cuda(),
            constructor
        )
        # Multi-GPU
        if not torch.cuda.device_count() > 1 or ignore_multidevice:
            return
        self._test_basic_cases_template(
            torch.randn(10, 5).cuda(0),
            torch.randn(10).cuda(1),
            torch.randn(5).cuda(0),
            constructor
        )

    def _build_params_dict(self, weight, bias, **kwargs):
        return [dict(params=[weight]), dict(params=[bias], **kwargs)]

    def _build_params_dict_single(self, weight, bias, **kwargs):
        return [dict(params=bias, **kwargs)]

    def test_sgd(self):
        self._test_rosenbrock(
            lambda params: optim.SGD(params, lr=1e-3),
            wrap_old_fn(old_optim.sgd, learningRate=1e-3)
        )
        self._test_rosenbrock(
            lambda params: optim.SGD(params, lr=1e-3, momentum=0.9,
                                     dampening=0, weight_decay=1e-4),
            wrap_old_fn(old_optim.sgd, learningRate=1e-3, momentum=0.9,
                        dampening=0, weightDecay=1e-4)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.SGD([weight, bias], lr=1e-3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.SGD(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.SGD(
                self._build_params_dict_single(weight, bias, lr=1e-2),
                lr=1e-3)
        )

    def test_sgd_sparse(self):
        self._test_rosenbrock_sparse(
            lambda params: optim.SGD(params, lr=5e-3)
        )

    def test_adam(self):
        self._test_rosenbrock(
            lambda params: optim.Adam(params, lr=1e-2),
            wrap_old_fn(old_optim.adam, learningRate=1e-2)
        )
        self._test_rosenbrock(
            lambda params: optim.Adam(params, lr=1e-2, weight_decay=1e-2),
            wrap_old_fn(old_optim.adam, learningRate=1e-2, weightDecay=1e-2)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adam([weight, bias], lr=1e-3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adam(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adam([weight, bias], lr=1e-3,
                                            amsgrad=True)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adam(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-3, amsgrad=True)
        )
        with self.assertRaisesRegex(ValueError, "Invalid beta parameter at index 0: 1.0"):
            optim.Adam(None, lr=1e-2, betas=(1.0, 0.0))

    def test_sparse_adam(self):
        self._test_rosenbrock_sparse(
            lambda params: optim.SparseAdam(params, lr=4e-2),
            True
        )

    def test_adadelta(self):
        self._test_rosenbrock(
            lambda params: optim.Adadelta(params),
            wrap_old_fn(old_optim.adadelta)
        )
        self._test_rosenbrock(
            lambda params: optim.Adadelta(params, rho=0.95),
            wrap_old_fn(old_optim.adadelta, rho=0.95)
        )
        self._test_rosenbrock(
            lambda params: optim.Adadelta(params, weight_decay=1e-2),
            wrap_old_fn(old_optim.adadelta, weightDecay=1e-2)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adadelta([weight, bias])
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adadelta(
                self._build_params_dict(weight, bias, rho=0.95))
        )

    def test_adagrad(self):
        self._test_rosenbrock(
            lambda params: optim.Adagrad(params, lr=1e-1),
            wrap_old_fn(old_optim.adagrad, learningRate=1e-1)
        )
        self._test_rosenbrock(
            lambda params: optim.Adagrad(params, lr=1e-1, lr_decay=1e-3),
            wrap_old_fn(old_optim.adagrad, learningRate=1e-1, learningRateDecay=1e-3)
        )
        self._test_rosenbrock(
            lambda params: optim.Adagrad(params, lr=1e-1, weight_decay=1e-2),
            wrap_old_fn(old_optim.adagrad, learningRate=1e-1, weightDecay=1e-2)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad([weight, bias], lr=1e-1)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-1)
        )

    def test_adagrad_sparse(self):
        self._test_rosenbrock_sparse(
            lambda params: optim.Adagrad(params, lr=1e-1)
        )

    def test_adamax(self):
        self._test_rosenbrock(
            lambda params: optim.Adamax(params, lr=1e-1),
            wrap_old_fn(old_optim.adamax, learningRate=1e-1)
        )
        self._test_rosenbrock(
            lambda params: optim.Adamax(params, lr=1e-1, weight_decay=1e-2),
            wrap_old_fn(old_optim.adamax, learningRate=1e-1, weightDecay=1e-2)
        )
        self._test_rosenbrock(
            lambda params: optim.Adamax(params, lr=1e-1, betas=(0.95, 0.998)),
            wrap_old_fn(old_optim.adamax, learningRate=1e-1, beta1=0.95, beta2=0.998)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad([weight, bias], lr=1e-1)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-1)
        )

    def test_rmsprop(self):
        self._test_rosenbrock(
            lambda params: optim.RMSprop(params, lr=1e-2),
            wrap_old_fn(old_optim.rmsprop, learningRate=1e-2)
        )
        self._test_rosenbrock(
            lambda params: optim.RMSprop(params, lr=1e-2, weight_decay=1e-2),
            wrap_old_fn(old_optim.rmsprop, learningRate=1e-2, weightDecay=1e-2)
        )
        self._test_rosenbrock(
            lambda params: optim.RMSprop(params, lr=1e-2, alpha=0.95),
            wrap_old_fn(old_optim.rmsprop, learningRate=1e-2, alpha=0.95)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad([weight, bias], lr=1e-2)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Adagrad(
                self._build_params_dict(weight, bias, lr=1e-3),
                lr=1e-2)
        )

    def test_asgd(self):
        self._test_rosenbrock(
            lambda params: optim.ASGD(params, lr=1e-3),
            wrap_old_fn(old_optim.asgd, eta0=1e-3)
        )
        self._test_rosenbrock(
            lambda params: optim.ASGD(params, lr=1e-3, alpha=0.8),
            wrap_old_fn(old_optim.asgd, eta0=1e-3, alpha=0.8)
        )
        self._test_rosenbrock(
            lambda params: optim.ASGD(params, lr=1e-3, t0=1e3),
            wrap_old_fn(old_optim.asgd, eta0=1e-3, t0=1e3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.ASGD([weight, bias], lr=1e-3, t0=100)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.ASGD(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-3, t0=100)
        )

    def test_rprop(self):
        self._test_rosenbrock(
            lambda params: optim.Rprop(params, lr=1e-3),
            wrap_old_fn(old_optim.rprop, stepsize=1e-3)
        )
        self._test_rosenbrock(
            lambda params: optim.Rprop(params, lr=1e-3, etas=(0.6, 1.1)),
            wrap_old_fn(old_optim.rprop, stepsize=1e-3, etaminus=0.6, etaplus=1.1)
        )
        self._test_rosenbrock(
            lambda params: optim.Rprop(params, lr=1e-3, step_sizes=(1e-4, 3)),
            wrap_old_fn(old_optim.rprop, stepsize=1e-3, stepsizemin=1e-4, stepsizemax=3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Rprop([weight, bias], lr=1e-3)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.Rprop(
                self._build_params_dict(weight, bias, lr=1e-2),
                lr=1e-3)
        )

    def test_lbfgs(self):
        self._test_rosenbrock(
            lambda params: optim.LBFGS(params),
            wrap_old_fn(old_optim.lbfgs)
        )
        self._test_rosenbrock(
            lambda params: optim.LBFGS(params, lr=5e-2, max_iter=5),
            wrap_old_fn(old_optim.lbfgs, learningRate=5e-2, maxIter=5)
        )
        self._test_basic_cases(
            lambda weight, bias: optim.LBFGS([weight, bias]),
            ignore_multidevice=True
        )

    def test_invalid_param_type(self):
        with self.assertRaises(TypeError):
            optim.SGD(Variable(torch.randn(5, 5)), lr=3)


class SchedulerTestNet(torch.nn.Module):
    def __init__(self):
        super(SchedulerTestNet, self).__init__()
        self.conv1 = torch.nn.Conv2d(1, 1, 1)
        self.conv2 = torch.nn.Conv2d(1, 1, 1)

    def forward(self, x):
        return self.conv2(F.relu(self.conv1(x)))


class TestLRScheduler(TestCase):
    def setUp(self):
        self.net = SchedulerTestNet()
        self.opt = SGD(
            [{'params': self.net.conv1.parameters()}, {'params': self.net.conv2.parameters(), 'lr': 0.5}],
            lr=0.05)

    def test_step_lr(self):
        # lr = 0.05     if epoch < 3
        # lr = 0.005    if 30 <= epoch < 6
        # lr = 0.0005   if epoch >= 9
        epochs = 10
        single_targets = [0.05] * 3 + [0.005] * 3 + [0.0005] * 3 + [0.00005] * 3
        targets = [single_targets, list(map(lambda x: x * epochs, single_targets))]
        scheduler = StepLR(self.opt, gamma=0.1, step_size=3)
        self._test(scheduler, targets, epochs)

    def test_multi_step_lr(self):
        # lr = 0.05     if epoch < 2
        # lr = 0.005    if 2 <= epoch < 5
        # lr = 0.0005   if epoch < 9
        # lr = 0.00005   if epoch >= 9
        epochs = 10
        single_targets = [0.05] * 2 + [0.005] * 3 + [0.0005] * 4 + [0.00005] * 3
        targets = [single_targets, list(map(lambda x: x * epochs, single_targets))]
        scheduler = MultiStepLR(self.opt, gamma=0.1, milestones=[2, 5, 9])
        self._test(scheduler, targets, epochs)

    def test_exp_lr(self):
        epochs = 10
        single_targets = [0.05 * (0.9 ** x) for x in range(epochs)]
        targets = [single_targets, list(map(lambda x: x * epochs, single_targets))]
        scheduler = ExponentialLR(self.opt, gamma=0.9)
        self._test(scheduler, targets, epochs)

    def test_cos_anneal_lr(self):
        epochs = 10
        eta_min = 1e-10
        single_targets = [eta_min + (0.05 - eta_min) *
                          (1 + math.cos(math.pi * x / epochs)) / 2
                          for x in range(epochs)]
        targets = [single_targets, list(map(lambda x: x * epochs, single_targets))]
        scheduler = CosineAnnealingLR(self.opt, T_max=epochs, eta_min=eta_min)
        self._test(scheduler, targets, epochs)

    def test_reduce_lr_on_plateau1(self):
        epochs = 10
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 20]
        metrics = [10 - i * 0.0167 for i in range(20)]
        scheduler = ReduceLROnPlateau(self.opt, threshold_mode='abs', mode='min',
                                      threshold=0.01, patience=5, cooldown=5)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau2(self):
        epochs = 22
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 6 + [0.05] * 7 + [0.005] * 7 + [0.0005] * 2]
        metrics = [10 - i * 0.0165 for i in range(22)]
        scheduler = ReduceLROnPlateau(self.opt, patience=5, cooldown=0, threshold_mode='abs',
                                      mode='min', threshold=0.1)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau3(self):
        epochs = 22
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * (2 + 6) + [0.05] * (5 + 6) + [0.005] * 4]
        metrics = [-0.8] * 2 + [-0.234] * 20
        scheduler = ReduceLROnPlateau(self.opt, mode='max', patience=5, cooldown=5,
                                      threshold_mode='abs')
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau4(self):
        epochs = 20
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 20]
        metrics = [1.5 * (1.025 ** i) for i in range(20)]  # 1.025 > 1.1**0.25
        scheduler = ReduceLROnPlateau(self.opt, mode='max', patience=3,
                                      threshold_mode='rel', threshold=0.1)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau5(self):
        epochs = 20
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 6 + [0.05] * (5 + 6) + [0.005] * 4]
        metrics = [1.5 * (1.005 ** i) for i in range(20)]
        scheduler = ReduceLROnPlateau(self.opt, mode='max', threshold_mode='rel',
                                      threshold=0.1, patience=5, cooldown=5)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau6(self):
        epochs = 20
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 20]
        metrics = [1.5 * (0.85 ** i) for i in range(20)]
        scheduler = ReduceLROnPlateau(self.opt, mode='min', threshold_mode='rel',
                                      threshold=0.1)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau7(self):
        epochs = 20
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 6 + [0.05] * (5 + 6) + [0.005] * 4]
        metrics = [1] * 7 + [0.6] + [0.5] * 12
        scheduler = ReduceLROnPlateau(self.opt, mode='min', threshold_mode='rel',
                                      threshold=0.1, patience=5, cooldown=5)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_reduce_lr_on_plateau8(self):
        epochs = 20
        for param_group in self.opt.param_groups:
            param_group['lr'] = 0.5
        targets = [[0.5] * 6 + [0.4] * 14, [0.5] * 6 + [0.3] * 14]
        metrics = [1.5 * (1.005 ** i) for i in range(20)]
        scheduler = ReduceLROnPlateau(self.opt, mode='max', threshold_mode='rel', min_lr=[0.4, 0.3],
                                      threshold=0.1, patience=5, cooldown=5)
        self._test_reduce_lr_on_plateau(scheduler, targets, metrics, epochs)

    def test_lambda_lr(self):
        epochs = 10
        self.opt.param_groups[0]['lr'] = 0.05
        self.opt.param_groups[1]['lr'] = 0.4
        targets = [[0.05 * (0.9 ** x) for x in range(epochs)], [0.4 * (0.8 ** x) for x in range(epochs)]]
        scheduler = LambdaLR(self.opt,
                             lr_lambda=[lambda x1: 0.9 ** x1, lambda x2: 0.8 ** x2])
        self._test(scheduler, targets, epochs)

    def _test(self, scheduler, targets, epochs=10):
        for epoch in range(epochs):
            scheduler.step(epoch)
            for param_group, target in zip(self.opt.param_groups, targets):
                self.assertAlmostEqual(target[epoch], param_group['lr'],
                                       msg='LR is wrong in epoch {}: expected {}, got {}'.format(
                                           epoch, target[epoch], param_group['lr']), delta=1e-5)

    def _test_reduce_lr_on_plateau(self, scheduler, targets, metrics, epochs=10, verbose=False):
        for epoch in range(epochs):
            scheduler.step(metrics[epoch])
            if verbose:
                print('epoch{}:\tlr={}'.format(epoch, self.opt.param_groups[0]['lr']))
            for param_group, target in zip(self.opt.param_groups, targets):
                self.assertAlmostEqual(target[epoch], param_group['lr'],
                                       msg='LR is wrong in epoch {}: expected {}, got {}'.format(
                                           epoch, target[epoch], param_group['lr']), delta=1e-5)


if __name__ == '__main__':
    run_tests()
