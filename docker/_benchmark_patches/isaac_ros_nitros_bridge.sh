# Patch for isaac_ros_nitros_bridge
# Issue: image_converter_node.cpp includes <boost/uuid/uuid.hpp> but the
# base only has minimal libboost-dev. Install the headers proper.

PATCH_EXTRA_APT="libboost-dev libboost-system-dev libboost-filesystem-dev"
