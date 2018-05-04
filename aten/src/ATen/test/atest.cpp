#include "ATen/ATen.h"
#include "test_assert.h"
#include "test_seed.h"

#include<iostream>
using namespace std;
using namespace at;

void check(bool c) {
  if(!c)
    throw std::runtime_error("check failed.");
}

void trace() {
  Tensor foo = rand(CPU(kFloat), {12,12});

  // ASSERT foo is 2-dimensional and holds floats.
  auto foo_a = foo.accessor<float,2>();
  float trace = 0;

  for(int i = 0; i < foo_a.size(0); i++) {
    trace += foo_a[i][i];
  }
  cout << trace << "\n" << foo << "\n";
}
int main() {
  manual_seed(123);

  auto foo = rand(CPU(kFloat), {12,6});
  ASSERT(foo.data<float>() == foo.toFloatData());

  cout << foo << "\n" << foo.size(0) << " " << foo.size(1) << endl;

  foo = foo+foo*3;
  foo -= 4;

  {
    Tensor no;
    ASSERT_THROWS(add_out(no,foo,foo));
  }
  Scalar a = 4;

  float b = a.to<float>();
  check(b == 4);

  foo = (foo*foo) == (foo.pow(3));
  foo =  2 + (foo+1);
  //foo = foo[3];
  auto foo_v = foo.accessor<uint8_t,2>();

  cout << foo_v.size(0) << " " << foo_v.size(1) << endl;
  for(int i = 0; i < foo_v.size(0); i++) {
    for(int j = 0; j < foo_v.size(1); j++) {
      //cout << foo_v[i][j] << " ";
      foo_v[i][j]++;
    }
    //cout << "\n";
  }


  cout << foo << "\n";

  trace();

  float data[] = { 1, 2, 3,
                   4, 5, 6};

  auto f = CPU(kFloat).tensorFromBlob(data, {1,2,3});

  cout << f << endl;
  cout << f.strides() << " " << f.sizes() << endl;
  ASSERT_THROWS(f.resize_({3,4,5}));
  {
    int isgone = 0;
    {
      auto f2 = CPU(kFloat).tensorFromBlob(data, {1,2,3}, [&](void*) {
        isgone++;
      });
      cout << f2 << endl;
    }
    check(isgone == 1);
  }
  {
    int isgone = 0;
    Tensor a_view;
    {
      auto f2 = CPU(kFloat).tensorFromBlob(data, {1,2,3}, [&](void*) {
        isgone++;
      });
      a_view = f2.view({3,2,1});
    }
    check(isgone == 0);
    a_view.reset();
    check(isgone == 1);
  }

  if(at::hasCUDA()) {
    int isgone = 0;
    {
      auto f2 = CUDA(kFloat).tensorFromBlob(nullptr, {1,2,3}, [&](void*) {
        isgone++;
      });
    }
    check(isgone==1);
  }


  return 0;
}
