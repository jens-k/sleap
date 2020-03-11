import logging
import sys

# Setup logging to stdout
logging.basicConfig(stream=sys.stdout, level=logging.INFO)

# Import submodules we want available at top-level
from sleap.io.dataset import Labels
from sleap.io.video import Video
from sleap.instance import LabeledFrame, Instance, PredictedInstance
from sleap.skeleton import Skeleton

from sleap.version import __version__
