#pragma once
#include <functional>
#include <memory>
#include <string>

#include "torch/csrc/jit/ir.h"
#include "torch/csrc/jit/script/error_report.h"
#include "torch/csrc/jit/script/tree_views.h"
#include "torch/csrc/jit/script/module.h"

namespace torch {
namespace jit {
namespace script {

// The AST can contain nodes like `self`, `self.b` or `python_fn` that
// are not first-class values in the graph representation, but instead
// will be desugared based on how they are used in the AST.

// SugaredValue is used to temporarily represent these values in a way
// that separates their behavior from AST -> IR converter itself.
// This allows us to keep dependencies on python minimal.

struct SugaredValue : public std::enable_shared_from_this<SugaredValue> {
  // what is this node? for error report (e.g. Module, python function)
  virtual std::string kind() const = 0;
  // what can we do with this thing?

  // use it as a value e.g.  `this + 4`
  virtual Value * asValue(SourceRange loc, Method & m) {
    throw ErrorReport(loc) << kind() << " cannot be used as a value";
  }

  // select an attribute on it, e.g. `this.field`
  virtual std::shared_ptr<SugaredValue> attr(SourceRange loc, Method & m, const std::string& field) {
    throw ErrorReport(loc) << "attribute lookup is not defined on " << kind();
  }

  // call it like a function, e.g. `outputs = this(inputs)`
  virtual std::vector<Value*> call(
    SourceRange loc,
    Method & m,
    at::ArrayRef<Value*> inputs,
    List<Attribute> attributes,
    size_t n_outputs) {
    throw ErrorReport(loc) << "cannot call a " << kind();
  }
  virtual ~SugaredValue() {}
};

// most things in the environment are just simple value types
// and not special python syntax sugar types
struct SimpleValue : public SugaredValue {
  SimpleValue(Value * value)
  : value(value) {}
  virtual std::string kind() const override {
    return "value";
  }
  virtual Value * asValue(SourceRange range, Method & m) override {
    return value;
  }
  virtual std::shared_ptr<SugaredValue> attr(SourceRange loc, Method & m, const std::string& field) override;

private:
  Value* value;
};

using Resolver = std::function<std::shared_ptr<SugaredValue>(const std::string& name)>;
void defineMethodsInModule(
  Module & m,
  const std::vector<Def>& definitions,
  const Resolver& resolver, /* determines how we handle free variables*/
  std::shared_ptr<SugaredValue> self /* if non-null, the first argument to each def, is bound to this value */
);

// same as above but parse the definitions from source
void defineMethodsInModule(Module & m, const std::string& source, const Resolver& resolver, std::shared_ptr<SugaredValue> self);

std::shared_ptr<Graph> compileFunction(Def def, const Resolver& resolver);


} // namespace script
} // namespace jit
} // namespace torch
