#include <Python.h>

#include "torch/csrc/jit/ir.h"
#include "torch/csrc/jit/pybind.h"
#include "torch/csrc/jit/python_tracer.h"
#include "torch/csrc/utils/pybind.h"

#include <iostream>
#include <sstream>

namespace torch { namespace jit {

void initPythonIRBindings(PyObject * module_) {
  auto m = py::handle(module_).cast<py::module>();
  #define GS(name) \
    def(#name,&Graph :: name)
  py::class_<Graph,std::shared_ptr<Graph>>(m,"Graph")
    .def(py::init<>())
    .def("__repr__",[](Graph & g) {
      std::stringstream ss;
      ss << g;
      return ss.str();
    })
    .def("inputs",[](Graph &g) {
      return py::make_iterator(g.inputs().begin(), g.inputs().end());
    })
    .def("outputs",[](Graph &g) {
      return py::make_iterator(g.outputs().begin(), g.outputs().end());
    })
    // TODO: Iterator invalidation might make this hazardous
    .def("nodes",[](Graph &g) {
      return py::make_iterator(g.nodes().begin(), g.nodes().end());
    })
    .def("addInput",[](Graph &g) { return g.addInput(); })
    .GS(advanceStage)
    .GS(stage)
    .GS(eraseInput)
    .GS(registerOutput)
    .def("create",[](Graph & g, const char * str) {
      return g.create(Symbol(str));
    })
    .def("create",[](Graph & g, const char * str, size_t noutputs) {
      return g.create(Symbol(str), noutputs);
    })
    .def("create",[](Graph & g, const char * str, const std::vector<Value*> & inputs) {
      return g.create(Symbol(str),inputs);
    })
    .def("create",[](Graph & g, const char * str, const std::vector<Value*> & inputs, size_t noutputs) {
      return g.create(Symbol(str),inputs, noutputs);
    })
    .GS(createConstant)
    .GS(createFusionGroup)
    .def("createClone",[](Graph & g, Node * n, py::object fn) {
      return g.createClone(n, [&](Value * e) {
        return fn(e).cast<Value*>();
      });
    })
    .GS(appendNode)
    .GS(prependNode)
    .GS(lint)
    ;
    #undef GS

  #define VS(name) \
    def(#name,&Value :: name)
  py::class_<Value,std::unique_ptr<Value, py::nodelete>>(m,"Value")
    .def("__repr__",[](Value & n) {
      std::stringstream ss;
      ss << n.uniqueName() << " defined in (" << *n.node() << ")";
      return ss.str();
    })
    .VS(type)
    .VS(setType)
    .VS(inferTypeFrom)
    // skip owningGraph because it returns a raw pointer to a otherwise
    // std::shared_ptr stored graph object, and would cause a double free
    .VS(unique)
    .VS(uniqueName)
    .VS(setUniqueName)
    .VS(setStage)
    .VS(stage)
    .VS(offset)
    .VS(uses)
    .VS(isHandle)
    .VS(replaceAllUsesWith)
    .def("node",[](Value &v) { return v.node(); })
    .def("setTypeAs", [](Value * node, Value * other) {
      node->setType(other->type());
      return node;
    })
    .VS(copyMetadata)
    ;

  #undef VS

  #define NS(name) \
    def(#name,&Node :: name)
  py::class_<Node,std::unique_ptr<Node, py::nodelete>>(m,"Node")
    .def("__repr__",[](Node & n) {
      std::stringstream ss;
      ss << n;
      return ss.str();
    })
    .def("hasMultipleOutputs",[](Node&n) {
      return n.outputs().size() > 1;
    })
    .def("outputsSize",[](Node &n) {
      return n.outputs().size();
    })
    .NS(kind)
    .NS(stage)
    .NS(setStage)
    .def("inputs",[](Node &n) {
      return py::make_iterator(n.inputs().begin(), n.inputs().end());
    })
    .def("outputs",[](Node &n) {
      return py::make_iterator(n.outputs().begin(), n.outputs().end());
    })
    .NS(output)
    .NS(addInput)
    .NS(replaceInput)
    .NS(replaceInputWith)
    .NS(replaceAllUsesWith)
    .NS(insertBefore)
    .NS(insertAfter)
    .NS(moveAfter)
    .NS(moveBefore)
    .NS(removeInput)
    .NS(removeAllInputs)
    .NS(destroy)
    .NS(hasUses)
    .NS(eraseOutput)
    .NS(addOutput)
    .NS(scopeName)

#define AS(name) def(#name,&Attributes<Node> :: name)
    // methods from Attributes
    .AS(copyAttributes)
    .AS(hasAttribute)
    .AS(kindOf)
    .AS(removeAttribute)
    .AS(hasAttributes)
    .AS(attributeNames)
#undef AS
#define CREATE_ACCESSOR(Kind,method) \
    def(#method "_",[](Node & n, const char * name, Kind##Attr::ValueType v) { \
      return n . method ## _(Symbol(name), std::move(v)); \
    }) \
    .def(#method, [](Node & n, const char * name) { \
      return n.method(Symbol(name)); \
    })
    .CREATE_ACCESSOR(Float,f)
    .CREATE_ACCESSOR(Floats,fs)
    .CREATE_ACCESSOR(String,s)
    .CREATE_ACCESSOR(Strings,ss)
    .CREATE_ACCESSOR(Int,i)
    .CREATE_ACCESSOR(Ints,is)
    .CREATE_ACCESSOR(Graph,g)
    .CREATE_ACCESSOR(Graphs,gs)
#undef CREATE_ACCESSOR
    // Tensor (t_) -- manually written to unwrap the variable into a tensor.
    .def("t_",[](Node & n, const char * name, torch::autograd::Variable v) {
      return n.t_(Symbol(name), std::move(v.data()));
    })
    .def("t", [](Node & n, const char * name) {
      return torch::autograd::make_variable(n.t(Symbol(name)), /*requires_grad=*/false);
    })
    // Tensors (ts_) -- manually written to unwrap variables into tensors.
    .def("ts_",[](Node & n, const char * name, std::vector<torch::autograd::Variable> vs) {
      std::vector<at::Tensor> tensors;
      tensors.reserve(vs.size());
      for (auto& variable : vs) {
        tensors.push_back(std::move(variable.data()));
      }
      return n.ts_(Symbol(name), std::move(tensors));
    })
    .def("ts", [](Node & n, const char * name) {
      auto tensors = n.ts(Symbol(name));
      std::vector<torch::autograd::Variable> variables;
      variables.reserve(tensors.size());
      for (auto& tensor : tensors) {
        variables.push_back(torch::autograd::make_variable(
            std::move(tensor), /*requires_grad=*/false));
      }
      return variables;
    })
    .def("z_",[](Node & n, const char * name, at::Tensor v) {
        return n.t_(Symbol(name), std::move(v.view({})));
    })
    .def("z",[](Node & n, const char * name) {
        return n.t(Symbol(name));
    })
    .def("zs_",[](Node & n, const char * name, TensorsAttr::ValueType v) {
        for (size_t i = 0; i < v.size(); ++ i) {
            v[i] = v[i].view({});
        }
        return n.ts_(Symbol(name), std::move(v));
    })
    .def("zs",[](Node & n, const char * name) {
        return n.ts(Symbol(name));
    })
    .def("pyobj",[](Node & n) {
      return py::handle(n.expect<PythonOp>()->pyobj.get()).cast<py::object>();
    })
    .def("cconv",[](Node & n) {
      return n.expect<PythonOp>()->cconv;
    })
    .def("pyname",[](Node & n) {
      return n.expect<PythonOp>()->name();
    })
    .def("scalar_args",[](Node & n) {
      auto op = n.expect<PythonOp>();
      auto scalars = py::list();
      auto append = scalars.attr("append");
      for(auto & arg : op->scalar_args) {
        append(py::handle(arg.get()));
      }
      return scalars;
    })
    ;

  #define TS(name) \
    def(#name,&Node :: name)
  py::class_<Type,std::shared_ptr<Type>>(m,"Type")
    .def("__repr__",[](Type & t) {
      std::stringstream ss;
      ss << t;
      return ss.str();
    })
    .def("kind",[](Type& t_) {
      Type * t = &t_;
      switch(t->kind()) {
        case TypeKind::HandleType:
          return "HandleType";
        case TypeKind::DynamicType:
          return "DynamicType";
        case TypeKind::TensorType:
          return "TensorType";
        default:
          torch::barf("unknown type kind");
          return "";
        }
    })
    .def("sizes",[](Type& t) {
      return t.expect<TensorType>()->sizes();
    })
    .def("strides",[](Type& t) {
      return t.expect<TensorType>()->strides();
    })
    .def("contiguous",[](Type& t) {
      return t.expect<TensorType>()->contiguous();
    })
    .def("scalarType",[](Type& t) {
      return at::toString(t.expect<TensorType>()->scalarType());
    })
    ;

  py::class_<Use>(m,"Use")
  .def_readonly("user",&Use::user)
  .def_readonly("offset",&Use::offset);

  m.def("_jit_get_graph", [](tracer::TracingState* s) {
    return s->graph;
  });
  m.def("_jit_is_tracing", [](const autograd::Variable& var) {
    return tracer::isTracing(var);
  });
}
}}
