import os
import argparse
from itertools import count
from ..autograd.utils import CodeTemplate, write
from ..autograd.gen_autograd import load_aten_declarations

template_path = os.path.join(os.path.dirname(__file__), 'templates')

ATEN_DISPATCH_H = CodeTemplate.from_file(template_path + '/aten_dispatch.h')
ATEN_DISPATCH_CPP = CodeTemplate.from_file(template_path + '/aten_dispatch.cpp')
ATEN_INTERNED_STRINGS_H = CodeTemplate.from_file(template_path + '/aten_interned_strings.h')

ATTR_METHOD_MAP = {
    'int64_t': 'i',
    'IntList': 'is',
    'Scalar': 't',
    'bool': 'i',
    'double': 'f',
    'std::array<bool,2>': 'is',
    'std::array<bool,3>': 'is',
    'std::array<bool,4>': 'is',
}

TYPE_CASTS = {
    'std::array<bool,2>': 'as_bool_array<2>',
    'std::array<bool,3>': 'as_bool_array<3>',
    'std::array<bool,4>': 'as_bool_array<4>',
    'Scalar': 'Scalar',
    'IntList': 'std::vector<int64_t>',
}

KW_ASSIGNMENT = CodeTemplate("""\
auto ${name} = ${type_cast}(node->${method}(Symbol("${name}")));\
""")

POS_ASSIGNMENT = CodeTemplate("""\
auto ${name} = tensor_as<${type}>(std::move(peek(stack, ${i}, ${N})));\
""")

CALL_NAMESPACE = CodeTemplate("at::${name}(${args})")
CALL_METHOD = CodeTemplate("(${first}).${name}(${args})")

CONSTRUCTOR = CodeTemplate("""\
{"${descriptor}", [](Node *node) {
  ${kw_assignments}
  return TensorOp([=](Stack & stack) {
    autograd::profiler::RecordFunction record("${name}");
    AutoGPU device_guard(deviceForInputs(stack, ${num_dynamic_inputs}));
    ${pos_assignments}
    auto result = ${call};
    drop(stack, ${num_dynamic_inputs});
    pack(stack, std::move(result));
    return 0;
  }, "${name}", ${num_dynamic_inputs});
}},
""")


def is_magic_method(api_name):
    return api_name.startswith('__') and api_name.endswith('__')


def is_jit_op(decl):
    uses_tensors = any(arg['simple_type'] in {'Tensor', 'TensorList'} for arg in decl['arguments']) or \
        'Tensor' in decl['method_of']
    return ((not decl['api_name'].endswith('_') or is_magic_method(decl['api_name'])) and
            not decl['name'].endswith('_out') and
            not any(arg['simple_type'] == 'Generator' for arg in decl['arguments']) and
            not any(arg['simple_type'] == 'SparseTensor' for arg in decl['arguments']) and
            not any(arg['simple_type'] == 'Storage' for arg in decl['arguments']) and
            not any(arg['simple_type'] == 'Type' for arg in decl['arguments']) and
            uses_tensors)


# Scalar overloads like add(Tensor self, Scalar other) are not supported atm.
# TODO: Why are they not supported?
skip_scalar_overload = {
    'lt-2': [1], 'gt-2': [1], 'le-2': [1], 'ge-2': [1], 'eq-2': [1], 'ne-2': [1],
    'pow-2': [0, 1], 'add-3': [1], 'sub-3': [1], 'mul-2': [1], 'div-2': [1],
    'fmod-2': [1], 'remainder-2': [1], '__and__-2': [1], '__or__-2': [1],
    '__iand__-2': [1], '__ior__-2': [1], '__xor__-2': [1], '__ixor__-2': [1],
    '__lshift__-2': [1], '__ilshift__-2': [1], '__rshift__-2': [1], '__irshift__-2': [1],
}


def gen_jit_dispatch(declarations, out):
    ops = {}

    def is_tensor_arg(arg):
        return arg['simple_type'] in {'Tensor', 'TensorList'}

    def get_invocation(decl, args):
        if 'namespace' in decl['method_of']:
            return CALL_NAMESPACE.substitute(name=decl['name'], args=args)
        else:
            return CALL_METHOD.substitute(name=decl['name'], first=args[0], args=args[1:])

    def emit_decl_variant(decl, is_positional_arg, has_tensorlist):
        # is_positional_arg is a boolean list the same length as decl['arguments']
        # that indicates if the argument should come from the postional list
        # of inputs. If false, the argument comes from the constant attributes
        kw_assignments = []
        attr_names = []
        pos_assignments = []
        arguments = []

        if has_tensorlist:
            kw_assignments.append('size_t varargs_length = node->inputs().size();')
            # arguments look like: [tensor list], arg1, arg2, arg3
            # we use peek(<i>, static_inputs) to read the non-vararg inputs
            # from the end of the stack
            static_inputs = sum(is_positional_arg) - 1
            num_dynamic_inputs = 'varargs_length'
        else:
            static_inputs = sum(is_positional_arg)
            num_dynamic_inputs = static_inputs

        real_inputs = count()
        for i, arg in enumerate(decl['arguments']):
            # XXX: we currently support only TensorList ops that have a TensorList as
            # the first argument, that is then followed by a number of positional args.
            if arg['simple_type'] == 'TensorList':
                arguments.append('peekSlice(stack, 0, varargs_length - {}, varargs_length)'.format(static_inputs))
            elif is_tensor_arg(arg):
                arguments.append('std::move(peek(stack, {}, {}))'.format(next(real_inputs), static_inputs))
            elif is_positional_arg[i]:
                assign = POS_ASSIGNMENT.substitute(type=arg['simple_type'],
                                                   name=arg['name'],
                                                   i=next(real_inputs),
                                                   N=static_inputs)
                pos_assignments.append(assign)
                arguments.append(arg['name'])
            else:
                assign = KW_ASSIGNMENT.substitute(type_cast=TYPE_CASTS.get(arg['simple_type'], arg['simple_type']),
                                                  name=arg['name'],
                                                  method=ATTR_METHOD_MAP[arg['simple_type']])
                kw_assignments.append(assign)
                attr_names.append(arg['name'])
                arguments.append(arg['name'])
        call = get_invocation(decl, arguments)

        # Descriptor is a unique identifier for a particular overload of an op.
        attr_names = sorted(attr_names)
        num_inputs = '*' if has_tensorlist else static_inputs
        descriptor = '-'.join([decl['name'], str(num_inputs)] + attr_names)

        # If there are two overloads with the same descriptor, that differ only by a type of a
        # single argument, where one of them takes a tensor, while another one takes an
        # at::Scalar as a positional scalar arg, then prefer the tensor overload.
        # It should get broadcasted correctly.
        if descriptor in skip_scalar_overload:
            if any(decl['arguments'][idx]['simple_type'] == 'Scalar'
                   for idx in skip_scalar_overload[descriptor]):
                return

        constructor = CONSTRUCTOR.substitute(descriptor=descriptor, name=decl['name'],
                                             call=call,
                                             kw_assignments=kw_assignments,
                                             pos_assignments=pos_assignments,
                                             num_dynamic_inputs=num_dynamic_inputs)

        assert descriptor not in ops, descriptor
        ops[descriptor] = constructor

    def emit_decl(decl):
        arguments = decl['arguments']
        has_tensorlist = any(arg['simple_type'] == 'TensorList' for arg in arguments)
        num_tensor_args = sum(map(is_tensor_arg, arguments))

        # we currently only support vararg tensor lists when they are the _first_ argument
        # and the only tensor argument
        if has_tensorlist and (num_tensor_args != 1 or arguments[0]['simple_type'] != 'TensorList'):
            return

        # Right now, we generate dispatch methods that either take all non-tensor arguments
        # as attributes, or don't use any attributes at all. In the future we might want to
        # have something in the middle too (might be useful for e.g. constant propagation
        # into attributes, as that would allow us to avoid reparsing tensors into scalar
        # args at every invocation).

        all_arguments_are_inputs = tuple(True for _ in arguments)
        only_tensors_are_inputs = tuple(is_tensor_arg(arg) for arg in arguments)

        # NB: if there are no scalar args then both options on LHS are equivalent, so deduplicate them.
        for variant in set([all_arguments_are_inputs, only_tensors_are_inputs]):
            emit_decl_variant(decl, variant, has_tensorlist)

    # We need to add methods implemented manually in TensorImpl
    tensor_impl_methods = [{
        'name': name,
        'api_name': name,
        'method_of': ['Tensor'],
        'arguments': [{'name': 'self', 'simple_type': 'Tensor'}],
    } for name in ['sizes', 'strides', 'dim']]
    aten_decls = load_aten_declarations(declarations) + tensor_impl_methods
    jit_decls = [d for d in aten_decls if is_jit_op(d)]

    for decl in jit_decls:
        emit_decl(decl)

    # Sort the generated snippets to ensure that the generation is deterministic
    env = {'constructors': sorted(ops.values())}
    write(out, 'aten_dispatch.h', ATEN_DISPATCH_H, env)
    write(out, 'aten_dispatch.cpp', ATEN_DISPATCH_CPP, env)

    names = set(decl['name'] for decl in jit_decls)
    strings_env = {'aten_symbols': ["_({}) \\".format(n) for n in sorted(names)]}

    write(out, 'aten_interned_strings.h', ATEN_INTERNED_STRINGS_H, strings_env)


def main():
    parser = argparse.ArgumentParser(
        description='Generate JIT op dispatch')
    parser.add_argument('declarations', metavar='DECL',
                        help='path to Declarations.yaml')
    parser.add_argument('out', metavar='OUT',
                        help='path to output directory')
    args = parser.parse_args()
    gen_jit_dispatch(args.declarations, args.out)


if __name__ == '__main__':
    main()
