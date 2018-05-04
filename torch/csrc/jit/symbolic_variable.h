#pragma once

#include "torch/csrc/jit/ir.h"

namespace torch { namespace jit {

struct SymbolicVariable {
  SymbolicVariable() : v(nullptr) {}
  /* implicit */ SymbolicVariable(Value * v) : v(v) {}
  // we allow implicit conversions to/from Value since
  // this type truly just provides more methods for value
  operator Value*() {
    return v;
  }
  static SymbolicVariable asNewInput(Graph & g, std::string name = "") {
    return g.addInput(name);
  }
  static SymbolicVariable asNewInput(Graph & g, TypePtr type) {
    return g.addInput()->setType(std::move(type));
  }
  const std::vector<int64_t>& sizes() {
    return v->type()->expect<TensorType>()->sizes();
  }
  void addAsOutput() {
    v->owningGraph()->registerOutput(v);
  }
  static std::vector<SymbolicVariable> create(Symbol kind, ArrayRef<SymbolicVariable> inputs,
                                 int num_outputs = 1,
                                 Node** created_node = nullptr,
                                 Graph * g = nullptr) {
      if(g == nullptr) {
        g = inputs.at(0).value()->owningGraph();
      }
      Node * n = g->insertNode(g->create(kind, num_outputs));
      for(auto i : inputs) {
        n->addInput(i.value());
      }
      if(created_node) {
        *created_node = n;
      }
      std::vector<SymbolicVariable> out;
      for(auto v : n->outputs()) {
        out.emplace_back(v);
      }
      return out;
  }
  static bool isConstInt(at::Scalar s, int32_t i) {
    // int32_t is safely convertible to both double and int64_t
    if(s.isFloatingPoint()) {
      return (double) i == s.toDouble();
    } else {
      return (int64_t) i == s.toLong();
    }
  }
  SymbolicVariable operator*(const SymbolicVariable rhs) const {
    return create(kmul, {*this, rhs})[0].typeLike(*this);
  }
  SymbolicVariable operator*(at::Scalar rhs) const {
    if(isConstInt(rhs, 1))
      return *this;
    Node * n;
    auto r = create(kmul, {*this}, 1, &n)[0];
    n->t_(kother, rhs.toTensor());
    return r;
  }
  SymbolicVariable operator+(const SymbolicVariable rhs) const {
    Node * n;
    auto r = create(kadd, {*this, rhs}, 1, &n)[0].typeLike(*this);
    n->t_(kalpha, at::Scalar(1).toTensor());
    return r;
  }
  SymbolicVariable operator+(at::Scalar rhs) const {
    Node * n;
    auto r = create(kadd, {*this}, 1, &n)[0].typeLike(*this);
    n->t_(kalpha, at::Scalar(1).toTensor());
    n->t_(kother, rhs.toTensor());
    return r;
  }
  SymbolicVariable operator-() const {
    return create(kneg, {*this})[0].typeLike(*this);
  }
  SymbolicVariable mm(const SymbolicVariable rhs) const {
    auto r = create(s("mm"), {*this, rhs})[0];
    return r;
  }
  SymbolicVariable t() const {
    auto r = create(s("t"), {*this})[0];
    return r;
  }
  SymbolicVariable sigmoid() const {
    return create(ksigmoid, {*this})[0].typeLike(*this);
  }
  SymbolicVariable tanh() const {
    return create(ktanh, {*this})[0].typeLike(*this);
  }
  std::vector<SymbolicVariable> chunk(int32_t chunks, uint32_t dim) const {
    Node * n;
    auto r = create(s("chunk"), { *this }, chunks, &n);
    n->i_(s("chunks"), chunks)
     ->i_(s("dim"), dim);
    return r;
  }
  SymbolicVariable narrow(int dim, int64_t start, int64_t length) const {
    Node * n;
    auto r = create(s("narrow"), { *this }, 1, &n)[0];
    n->i_(s("dim"), dim)
     ->i_(s("start"), start)
     ->i_(s("length"), length);
    return r;
  }
  static SymbolicVariable cat(ArrayRef<SymbolicVariable> inputs, int32_t dim) {
    Node* n;
    auto r = create(kcat, inputs, 1, &n)[0];
    n->i_(kdim, dim);
    return r;
  }
  SymbolicVariable sum() const {
    auto r = create(s("sum"), {*this})[0];
    return r;
  }
  SymbolicVariable sum(int dim, bool keepdim) const {
    Node * n;
    auto r = create(s("sum"), {*this}, 1, &n)[0];
    n->i_(s("dim"), dim)
     ->i_(s("keepdim"), keepdim);
    return r;
  }
  SymbolicVariable squeeze(int dim) const {
    Node * n;
    auto r = create(s("squeeze"), {*this}, 1, &n)[0];
    n->i_(s("dim"), dim);
    return r;
  }
  SymbolicVariable unsqueeze(int dim) const {
    Node * n;
    auto r = create(s("unsqueeze"), {*this}, 1, &n)[0];
    n->i_(s("dim"), dim);
    return r;
  }
  SymbolicVariable view(std::vector<std::int64_t> sizes) const {
    Node *n;
    auto r =  create(kview, {*this}, 1, &n)[0];
    n->is_(s("size"), std::move(sizes));
    return r;
  }
  Value * value() const {
    return v;
  }
private:
  SymbolicVariable typeLike(SymbolicVariable other) {
    if (auto other_type = other.v->type()->cast<TensorType>())
      v->setType(other_type->contiguous());
    return *this;
  }
  static Symbol s(const char * s_) {
    return Symbol(s_);
  }
  Value * v;
};

// shorter method so that toVar(v) + toVar(c) is short.
static inline SymbolicVariable toVar(Value * v) {
  return SymbolicVariable(v);
}

template<typename T, typename = typename std::enable_if<std::is_arithmetic<T>::value>::type>
inline SymbolicVariable operator+(T lhs, SymbolicVariable rhs) {
  return rhs + at::Scalar(lhs);
}

inline SymbolicVariable operator+(at::Scalar lhs, SymbolicVariable rhs) {
  return rhs + lhs;
}

inline SymbolicVariable operator-(at::Scalar lhs, SymbolicVariable rhs) {
  return (lhs + (-rhs));
}

}}
