#include "InitMethod.hpp"

namespace thd {
namespace init {

InitMethod::Config initTCP(std::string argument, rank_type world_size,
                           std::string group_name, int rank);
InitMethod::Config initFile(std::string argument, rank_type world_size,
                            std::string group_name, int rank);
InitMethod::Config initEnv(int world_size, std::string group_name, int rank);

}

InitMethod::Config getInitConfig(std::string argument, int world_size,
                                 std::string group_name, int rank) {
  InitMethod::Config config;
  if (argument.find("env://") == 0) {
    config = init::initEnv(world_size, group_name, rank);
  } else {
    rank_type r_world_size;
    try {
      r_world_size = convertToRank(world_size);
    } catch(std::exception& e) {
      if (rank == -1)
        throw std::invalid_argument("world_size is not set - it is required for "
                                    "`tcp://` and `file://` init methods with this backend");
      throw std::invalid_argument("invalid world_size");
    }

    group_name.append("#"); // To make sure it's not empty

    if (argument.find("tcp://") == 0) {
      argument.erase(0, 6); // chop "tcp://"
      config = init::initTCP(argument, r_world_size, group_name, rank);
    } else if (argument.find("file://") == 0) {
      argument.erase(0, 7); // chop "file://"
      config = init::initFile(argument, r_world_size, group_name, rank);
    }
  }

  config.validate();
  return config;
}

} // namespace thd
