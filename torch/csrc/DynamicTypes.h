#pragma once

// Provides conversions between Python tensor objects and at::Tensor.

#include <Python.h>
#include <memory>
#include <unordered_map>
#include <ATen/ATen.h>
#include "torch/csrc/Dtype.h"

namespace torch {

// Register a PyTypeObject* with the given attributes
void registerStoragePyTypeObject(
    PyTypeObject *pytype, const std::string& name,
    bool is_cuda, bool is_sparse);

void registerDtypeObject(THPDtype *dtype, at::Backend backend, at::ScalarType scalarType, const at::Type* type);

PyObject* createPyObject(const at::Storage& storage);
THPDtype* getDtype(const at::Type& type);
THPDtype* getDtype(at::Backend backend, at::ScalarType scalarType);
std::unique_ptr<at::Storage> createStorage(PyObject* obj);

bool isStorage(PyObject* obj);

}  // namespace torch
