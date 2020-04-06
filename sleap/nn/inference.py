"""Inference pipelines and utilities."""

import attr
import logging
import os
import time
from collections import defaultdict
from typing import Text, Optional, List, Dict

import tensorflow as tf
import numpy as np

import sleap
from sleap import util
from sleap.nn.config import TrainingJobConfig
from sleap.nn.model import Model
from sleap.nn.tracking import Tracker
from sleap.nn.data.pipelines import (
    Provider,
    Pipeline,
    LabelsReader,
    VideoReader,
    Normalizer,
    Resizer,
    Prefetcher,
    LambdaFilter,
    KerasModelPredictor,
    LocalPeakFinder,
    PredictedInstanceCropper,
    InstanceCentroidFinder,
    InstanceCropper,
    GlobalPeakFinder,
    MockGlobalPeakFinder,
    KeyFilter,
    KeyRenamer,
    PredictedCenterInstanceNormalizer,
    PartAffinityFieldInstanceGrouper,
    PointsRescaler,
)

logger = logging.getLogger(__name__)


def group_examples(examples):
    """
    Group examples into dictionary.

    Key is (video_ind, frame_ind), val is list of examples matching key.
    """
    grouped_examples = defaultdict(list)
    for example in examples:
        video_ind = example["video_ind"].numpy()
        frame_ind = example["frame_ind"].numpy()
        grouped_examples[(video_ind, frame_ind)].append(example)
    return grouped_examples


def group_examples_iter(examples):
    """
    Iterator which groups examples.

    Yields ((video_ind, frame_ind), list of examples matching vid/frame).
    """
    last_key = None
    batch = []
    for example in examples:
        video_ind = example["video_ind"].numpy()
        frame_ind = example["frame_ind"].numpy()
        key = (video_ind, frame_ind)

        if last_key != key:
            if batch:
                yield last_key, batch
            last_key = key
            batch = [example]
        else:
            batch.append(example)

    if batch:
        yield key, batch


def safely_generate(ds: tf.data.Dataset, progress: bool = True):
    """Yields examples from dataset, catching and logging exceptions."""

    # Unsafe generating:
    # for example in ds:
    #     yield example

    ds_iter = iter(ds)

    i = 0
    t0 = time.time()
    done = False
    while not done:
        try:
            yield next(ds_iter)
        except StopIteration:
            done = True
        except Exception as e:
            logger.info(f"ERROR in sample index {i}")
            logger.info(e)
            logger.info("")
        finally:
            if not done:
                i += 1

            # Show the current progress (frames, time, fps)
            if progress:
                if (i and i % 1000 == 0) or done:
                    elapsed_time = time.time() - t0
                    logger.info(f"Finished {i} frames in {elapsed_time:.2f} seconds")
                    if elapsed_time:
                        logger.info(f"FPS={i/elapsed_time}")


def make_grouped_labeled_frame(
    video_ind: int,
    frame_ind: int,
    frame_examples: List[Dict[Text, tf.Tensor]],
    videos: List[sleap.Video],
    skeleton: "Skeleton",
    points_key: Text,
    point_confidences_key: Text,
    image_key: Optional[Text] = None,
    instance_score_key: Optional[Text] = None,
    tracker: Optional[Tracker] = None,
) -> List[sleap.LabeledFrame]:

    predicted_frames = []

    # Create predicted instances from examples in the current frame.
    predicted_instances = []
    img = None
    for example in frame_examples:
        if instance_score_key is None:
            instance_scores = np.nansum(example[point_confidences_key])
        else:
            instance_scores = example[instance_score_key]

        if example[points_key].ndim == 3:
            for points, confidences, instance_score in zip(
                example[points_key], example[point_confidences_key], instance_scores,
            ):
                predicted_instances.append(
                    sleap.PredictedInstance.from_arrays(
                        points=points,
                        point_confidences=confidences,
                        instance_score=instance_score,
                        skeleton=skeleton,
                    )
                )
        else:
            points = example[points_key]
            confidences = example[point_confidences_key]
            instance_score = instance_scores

            predicted_instances.append(
                sleap.PredictedInstance.from_arrays(
                    points=points,
                    point_confidences=confidences,
                    instance_score=instance_score,
                    skeleton=skeleton,
                )
            )

        if image_key is not None and image_key in example:
            img = example[image_key]
        else:
            img = None

    if len(predicted_instances) > 0:
        if tracker:
            # Set tracks for predicted instances in this frame.
            predicted_instances = tracker.track(
                untracked_instances=predicted_instances, img=img, t=frame_ind,
            )

        # Create labeled frame from predicted instances.
        labeled_frame = sleap.LabeledFrame(
            video=videos[video_ind], frame_idx=frame_ind, instances=predicted_instances,
        )

        predicted_frames.append(labeled_frame)

    return predicted_frames


def get_keras_model_path(path: Text) -> Text:
    if path.endswith(".json"):
        path = os.path.dirname(path)
    return os.path.join(path, "best_model.h5")


@attr.s(auto_attribs=True)
class VisualPredictor:
    config: TrainingJobConfig
    model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(cls, model_path: Text) -> "VisualPredictor":
        cfg = TrainingJobConfig.load_json(model_path)
        keras_model_path = get_keras_model_path(model_path)
        model = Model.from_config(cfg.model)
        model.keras_model = tf.keras.models.load_model(keras_model_path, compile=False)

        return cls(config=cfg, model=model)

    def head_specific_output_keys(self) -> List[Text]:
        keys = []

        key = self.confidence_maps_key_name()
        if key:
            keys.append(key)

        key = self.part_affinity_fields_key_name()
        if key:
            keys.append(key)

        return keys

    def confidence_maps_key_name(self) -> Optional[Text]:
        head_key = self.config.model.heads.which_oneof_attrib_name()

        if head_key in ("multi_instance", "single_instance"):
            return "predicted_confidence_maps"

        if head_key == "centroid":
            return "predicted_centroid_confidence_maps"

        # todo: centered_instance

        return None

    def part_affinity_fields_key_name(self) -> Optional[Text]:
        head_key = self.config.model.heads.which_oneof_attrib_name()

        if head_key == "multi_instance":
            return "predicted_part_affinity_fields"

        return None

    def make_pipeline(self):
        pipeline = Pipeline()

        pipeline += Normalizer.from_config(self.config.data.preprocessing)
        pipeline += Resizer.from_config(
            self.config.data.preprocessing, keep_full_image=False, points_key=None,
        )

        pipeline += KerasModelPredictor(
            keras_model=self.model.keras_model,
            model_input_keys="image",
            model_output_keys=self.head_specific_output_keys(),
        )

        self.pipeline = pipeline

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            # Pass in data provider when mocking one of the models.
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        # Yield each example from dataset, catching and logging exceptions
        return safely_generate(self.pipeline.make_dataset())

    def predict(self, data_provider: Provider):
        generator = self.predict_generator(data_provider)
        examples = list(generator)

        return examples


@attr.s(auto_attribs=True)
class TopdownPredictor:
    centroid_config: Optional[TrainingJobConfig] = attr.ib(default=None)
    centroid_model: Optional[Model] = attr.ib(default=None)
    confmap_config: Optional[TrainingJobConfig] = attr.ib(default=None)
    confmap_model: Optional[Model] = attr.ib(default=None)
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)
    tracker: Optional[Tracker] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(
        cls,
        centroid_model_path: Optional[Text] = None,
        confmap_model_path: Optional[Text] = None,
    ) -> "TopdownPredictor":
        """Create predictor from saved models.

        Args:
            centroid_model_path: Path to centroid model folder.
            confmap_model_path: Path to topdown confidence map model folder.
        
        Returns:
            An instance of TopdownPredictor with the loaded models.

            One of the two models can be left as None to perform inference with ground
            truth data. This will only work with LabelsReader as the provider.
        """
        if centroid_model_path is None and confmap_model_path is None:
            raise ValueError(
                "Either the centroid or topdown confidence map model must be provided."
            )

        if centroid_model_path is not None:
            # Load centroid model.
            centroid_config = TrainingJobConfig.load_json(centroid_model_path)
            centroid_keras_model_path = get_keras_model_path(centroid_model_path)
            centroid_model = Model.from_config(centroid_config.model)
            centroid_model.keras_model = tf.keras.models.load_model(
                centroid_keras_model_path, compile=False
            )
        else:
            centroid_config = None
            centroid_model = None

        if confmap_model_path is not None:
            # Load confmap model.
            confmap_config = TrainingJobConfig.load_json(confmap_model_path)
            confmap_keras_model_path = get_keras_model_path(confmap_model_path)
            confmap_model = Model.from_config(confmap_config.model)
            confmap_model.keras_model = tf.keras.models.load_model(
                confmap_keras_model_path, compile=False
            )
        else:
            confmap_config = None
            confmap_model = None

        return cls(
            centroid_config=centroid_config,
            centroid_model=centroid_model,
            confmap_config=confmap_config,
            confmap_model=confmap_model,
        )

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:

        keep_full_image = self.tracker and self.tracker.uses_image
        print(f"keep_full_image={keep_full_image}")

        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Prefetcher()

        pipeline += KeyRenamer(
            old_key_names=["image", "scale"],
            new_key_names=["full_image", "full_image_scale"],
            drop_old=False,
        )
        if self.confmap_config is not None:
            pipeline += Normalizer.from_config(
                self.confmap_config.data.preprocessing, image_key="full_image"
            )

            points_key = "instances" if self.centroid_model is None else None
            pipeline += Resizer.from_config(
                self.confmap_config.data.preprocessing,
                keep_full_image=keep_full_image,
                points_key=points_key,
                image_key="full_image",
                scale_key="full_image_scale",
            )

        if self.centroid_model is not None:
            pipeline += Normalizer.from_config(
                self.centroid_config.data.preprocessing, image_key="image"
            )
            pipeline += Resizer.from_config(
                self.centroid_config.data.preprocessing,
                keep_full_image=keep_full_image,
                points_key=None,
            )

            # Predict centroids using model.
            pipeline += KerasModelPredictor(
                keras_model=self.centroid_model.keras_model,
                model_input_keys="image",
                model_output_keys="predicted_centroid_confidence_maps",
            )

            pipeline += LocalPeakFinder(
                confmaps_stride=self.centroid_model.heads[0].output_stride,
                peak_threshold=0.2,
                confmaps_key="predicted_centroid_confidence_maps",
                peaks_key="predicted_centroids",
                peak_vals_key="predicted_centroid_confidences",
                peak_sample_inds_key="predicted_centroid_sample_inds",
                peak_channel_inds_key="predicted_centroid_channel_inds",
                keep_confmaps=False,
            )

            pipeline += LambdaFilter(
                filter_fn=lambda ex: len(ex["predicted_centroids"]) > 0
            )

            if self.confmap_config is not None:
                crop_size = self.confmap_config.data.instance_cropping.crop_size
            else:
                crop_size = sleap.nn.data.instance_cropping.find_instance_crop_size(
                    data_provider.labels
                )

            pipeline += PredictedInstanceCropper(
                crop_width=crop_size,
                crop_height=crop_size,
                centroids_key="predicted_centroids",
                centroid_confidences_key="predicted_centroid_confidences",
                full_image_key="full_image",
                full_image_scale_key="full_image_scale",
                keep_instances_gt=self.confmap_model is None,
                keep_full_image=keep_full_image,
            )

        else:
            # Generate ground truth centroids and crops.
            anchor_part = self.confmap_config.data.instance_cropping.center_on_part
            pipeline += InstanceCentroidFinder(
                center_on_anchor_part=True,
                anchor_part_names=anchor_part,
                skeletons=data_provider.labels.skeletons,
            )
            pipeline += KeyRenamer(
                old_key_names=["full_image", "full_image_scale"],
                new_key_names=["image", "scale"],
                drop_old=True,
            )
            pipeline += InstanceCropper(
                crop_width=self.confmap_config.data.instance_cropping.crop_size,
                crop_height=self.confmap_config.data.instance_cropping.crop_size,
                keep_full_image=keep_full_image,
                mock_centroid_confidence=True,
            )

        if self.confmap_model is not None:
            # Predict confidence maps using model.
            pipeline += KerasModelPredictor(
                keras_model=self.confmap_model.keras_model,
                model_input_keys="instance_image",
                model_output_keys="predicted_instance_confidence_maps",
            )
            pipeline += GlobalPeakFinder(
                confmaps_key="predicted_instance_confidence_maps",
                peaks_key="predicted_center_instance_points",
                confmaps_stride=self.confmap_model.heads[0].output_stride,
                peak_threshold=0.2,
            )

        else:
            # Generate ground truth instance points.
            pipeline += MockGlobalPeakFinder(
                all_peaks_in_key="instances",
                peaks_out_key="predicted_center_instance_points",
                peak_vals_key="predicted_center_instance_confidences",
                keep_confmaps=False,
            )

        keep_keys = [
            "bbox",
            "center_instance_ind",
            "centroid",
            "centroid_confidence",
            "scale",
            "video_ind",
            "frame_ind",
            "center_instance_ind",
            "predicted_center_instance_points",
            "predicted_center_instance_confidences",
        ]

        if keep_full_image:
            keep_keys.append("full_image")

        pipeline += KeyFilter(keep_keys=keep_keys)

        pipeline += PredictedCenterInstanceNormalizer(
            centroid_key="centroid",
            centroid_confidence_key="centroid_confidence",
            peaks_key="predicted_center_instance_points",
            peak_confidences_key="predicted_center_instance_confidences",
            new_centroid_key="predicted_centroid",
            new_centroid_confidence_key="predicted_centroid_confidence",
            new_peaks_key="predicted_instance",
            new_peak_confidences_key="predicted_instance_confidences",
        )

        self.pipeline = pipeline

        return pipeline

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            if self.centroid_config is not None and self.confmap_config is not None:
                self.make_pipeline()
            else:
                # Pass in data provider when mocking one of the models.
                self.make_pipeline(data_provider=data_provider)

        self.pipeline.providers = [data_provider]

        # Yield each example from dataset, catching and logging exceptions
        return safely_generate(self.pipeline.make_dataset())

    def make_labeled_frames_from_generator(self, generator, data_provider):
        grouped_generator = group_examples_iter(generator)

        if self.confmap_config is not None:
            skeleton = self.confmap_config.data.labels.skeletons[0]
        else:
            skeleton = self.centroid_config.data.labels.skeletons[0]

        def make_lfs(video_ind, frame_ind, frame_examples):
            return make_grouped_labeled_frame(
                video_ind=video_ind,
                frame_ind=frame_ind,
                frame_examples=frame_examples,
                videos=data_provider.videos,
                skeleton=skeleton,
                image_key="full_image",
                points_key="predicted_instance",
                point_confidences_key="predicted_instance_confidences",
                instance_score_key="predicted_centroid_confidence",
                tracker=self.tracker,
            )

        predicted_frames = []
        for (video_ind, frame_ind), grouped_examples in grouped_generator:
            predicted_frames.extend(make_lfs(video_ind, frame_ind, grouped_examples))

        if self.tracker:
            self.tracker.final_pass(predicted_frames)

        return predicted_frames

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)

        if make_instances:
            return self.make_labeled_frames_from_generator(generator, data_provider)

        return list(generator)


@attr.s(auto_attribs=True)
class BottomupPredictor:
    bottomup_config: TrainingJobConfig
    bottomup_model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)
    tracker: Optional[Tracker] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(cls, bottomup_model_path: Text) -> "BottomupPredictor":
        """Create predictor from saved models."""
        # Load bottomup model.
        bottomup_config = TrainingJobConfig.load_json(bottomup_model_path)
        bottomup_keras_model_path = get_keras_model_path(bottomup_model_path)
        bottomup_model = Model.from_config(bottomup_config.model)
        bottomup_model.keras_model = tf.keras.models.load_model(
            bottomup_keras_model_path, compile=False
        )

        return cls(bottomup_config=bottomup_config, bottomup_model=bottomup_model,)

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:
        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Normalizer.from_config(self.bottomup_config.data.preprocessing)
        pipeline += Resizer.from_config(
            self.bottomup_config.data.preprocessing,
            keep_full_image=False,
            points_key=None,
        )

        pipeline += Prefetcher()

        pipeline += KerasModelPredictor(
            keras_model=self.bottomup_model.keras_model,
            model_input_keys="image",
            model_output_keys=[
                "predicted_confidence_maps",
                "predicted_part_affinity_fields",
            ],
        )
        pipeline += LocalPeakFinder(
            confmaps_stride=self.bottomup_model.heads[0].output_stride,
            peak_threshold=0.2,
            confmaps_key="predicted_confidence_maps",
            peaks_key="predicted_peaks",
            peak_vals_key="predicted_peak_confidences",
            peak_sample_inds_key="predicted_peak_sample_inds",
            peak_channel_inds_key="predicted_peak_channel_inds",
            keep_confmaps=False,
        )

        pipeline += LambdaFilter(filter_fn=lambda ex: len(ex["predicted_peaks"]) > 0)

        pipeline += PartAffinityFieldInstanceGrouper.from_config(
            self.bottomup_config.model.heads.multi_instance,
            max_edge_length=128,
            min_edge_score=0.05,
            n_points=10,
            min_instance_peaks=0,
            peaks_key="predicted_peaks",
            peak_scores_key="predicted_peak_confidences",
            channel_inds_key="predicted_peak_channel_inds",
            pafs_key="predicted_part_affinity_fields",
            predicted_instances_key="predicted_instances",
            predicted_peak_scores_key="predicted_peak_scores",
            predicted_instance_scores_key="predicted_instance_scores",
            keep_pafs=False,
        )

        keep_keys = [
            "scale",
            "video_ind",
            "frame_ind",
            "predicted_instances",
            "predicted_peak_scores",
            "predicted_instance_scores",
        ]

        if self.tracker and self.tracker.uses_image:
            keep_keys.append("image")

        pipeline += KeyFilter(keep_keys=keep_keys)

        pipeline += PointsRescaler(
            points_key="predicted_instances", scale_key="scale", invert=True
        )

        self.pipeline = pipeline

        return pipeline

    def make_labeled_frames_from_generator(self, generator, data_provider):
        grouped_generator = group_examples_iter(generator)

        skeleton = self.bottomup_config.data.labels.skeletons[0]

        def make_lfs(video_ind, frame_ind, frame_examples):
            return make_grouped_labeled_frame(
                video_ind=video_ind,
                frame_ind=frame_ind,
                frame_examples=frame_examples,
                videos=data_provider.videos,
                skeleton=skeleton,
                image_key="image",
                points_key="predicted_instances",
                point_confidences_key="predicted_peak_scores",
                instance_score_key="predicted_instance_scores",
                tracker=self.tracker,
            )

        predicted_frames = []
        for (video_ind, frame_ind), grouped_examples in grouped_generator:
            predicted_frames.extend(make_lfs(video_ind, frame_ind, grouped_examples))

        if self.tracker:
            self.tracker.final_pass(predicted_frames)

        return predicted_frames

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        # Yield each example from dataset, catching and logging exceptions
        return safely_generate(self.pipeline.make_dataset())

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)

        if make_instances:
            return self.make_labeled_frames_from_generator(generator, data_provider)

        return list(generator)


@attr.s(auto_attribs=True)
class SingleInstancePredictor:
    confmap_config: TrainingJobConfig
    confmap_model: Model
    pipeline: Optional[Pipeline] = attr.ib(default=None, init=False)

    @classmethod
    def from_trained_models(cls, confmap_model_path: Text) -> "SingleInstancePredictor":
        """Create predictor from saved models."""

        # Load confmap model.
        confmap_config = TrainingJobConfig.load_json(confmap_model_path)
        confmap_keras_model_path = get_keras_model_path(confmap_model_path)
        confmap_model = Model.from_config(confmap_config.model)
        confmap_model.keras_model = tf.keras.models.load_model(
            confmap_keras_model_path, compile=False
        )

        return cls(confmap_config=confmap_config, confmap_model=confmap_model,)

    def make_pipeline(self, data_provider: Optional[Provider] = None) -> Pipeline:

        pipeline = Pipeline()
        if data_provider is not None:
            pipeline.providers = [data_provider]

        pipeline += Normalizer.from_config(self.confmap_config.data.preprocessing)
        pipeline += Resizer.from_config(
            self.confmap_config.data.preprocessing,
            # keep_full_image=True,
            points_key=None,
        )

        pipeline += Prefetcher()

        pipeline += KerasModelPredictor(
            keras_model=self.confmap_model.keras_model,
            model_input_keys="image",
            model_output_keys="predicted_instance_confidence_maps",
        )
        pipeline += GlobalPeakFinder(
            confmaps_key="predicted_instance_confidence_maps",
            peaks_key="predicted_instance",
            peak_vals_key="predicted_instance_confidences",
            confmaps_stride=self.confmap_model.heads[0].output_stride,
            peak_threshold=0.2,
        )

        pipeline += KeyFilter(
            keep_keys=[
                "scale",
                "video_ind",
                "frame_ind",
                "predicted_instance",
                "predicted_instance_confidences",
            ]
        )

        self.pipeline = pipeline

        return pipeline

    def make_labeled_frames_from_generator(self, generator, data_provider):
        grouped_generator = group_examples_iter(generator)

        skeleton = self.confmap_config.data.labels.skeletons[0]

        def make_lfs(video_ind, frame_ind, frame_examples):
            return make_grouped_labeled_frame(
                video_ind=video_ind,
                frame_ind=frame_ind,
                frame_examples=frame_examples,
                videos=data_provider.videos,
                skeleton=skeleton,
                points_key="predicted_instance",
                point_confidences_key="predicted_instance_confidences",
            )

        predicted_frames = []
        for (video_ind, frame_ind), grouped_examples in grouped_generator:
            predicted_frames.extend(make_lfs(video_ind, frame_ind, grouped_examples))

        return predicted_frames

    def predict_generator(self, data_provider: Provider):
        if self.pipeline is None:
            self.make_pipeline()

        self.pipeline.providers = [data_provider]

        # Yield each example from dataset, catching and logging exceptions
        return safely_generate(self.pipeline.make_dataset())

    def predict(self, data_provider: Provider, make_instances: bool = True):
        generator = self.predict_generator(data_provider)

        if make_instances:
            return self.make_labeled_frames_from_generator(generator, data_provider)

        return list(generator)


def make_cli_parser():
    import argparse
    from sleap.util import frame_list

    parser = argparse.ArgumentParser()

    # Add args for entire pipeline
    parser.add_argument("data_path", help="Path to video file")
    parser.add_argument(
        "-m",
        "--model",
        dest="models",
        action="append",
        help="Path to trained model directory (with training_config.json). "
        "Multiple models can be specified, each preceded by --model.",
    )

    parser.add_argument(
        "--frames",
        type=frame_list,
        default="",
        help="List of frames to predict. Either comma separated list (e.g. 1,2,3) or "
        "a range separated by hyphen (e.g. 1-3, for 1,2,3). (default is entire video)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=str,
        default=None,
        help="The output filename to use for the predicted data.",
    )

    # TODO: better video parameters

    parser.add_argument(
        "--video.dataset", type=str, default="", help="The dataset for HDF5 videos."
    )

    parser.add_argument(
        "--video.input_format",
        type=str,
        default="",
        help="The input_format for HDF5 videos.",
    )

    device_group = parser.add_mutually_exclusive_group(required=False)
    device_group.add_argument(
        "--cpu",
        action="store_true",
        help="Run inference only on CPU. If not specified, will use available GPU.",
    )
    device_group.add_argument(
        "--first-gpu",
        action="store_true",
        help="Run inference on the first GPU, if available.",
    )
    device_group.add_argument(
        "--last-gpu",
        action="store_true",
        help="Run inference on the last GPU, if available.",
    )
    device_group.add_argument(
        "--gpu", type=int, default=0, help="Run inference on the i-th GPU specified."
    )

    # Add args for tracking
    Tracker.add_cli_parser_args(parser, arg_scope="tracking")

    return parser


def make_video_reader_from_cli(args):
    # TODO: better support for video params
    video_kwargs = dict(
        dataset=vars(args).get("video.dataset"),
        input_format=vars(args).get("video.input_format"),
    )

    video_reader = VideoReader.from_filepath(
        filename=args.data_path, example_indices=args.frames, **video_kwargs
    )

    return video_reader


def make_predictor_from_cli(args):
    # trained_model_configs = dict()
    trained_model_paths = dict()

    head_names = (
        "single_instance",
        "centroid",
        "centered_instance",
        "multi_instance",
    )

    for model_path in args.models:
        # Load the model config
        cfg = TrainingJobConfig.load_json(model_path)

        # Get the head from the model (i.e., what the model will predict)
        key = cfg.model.heads.which_oneof_attrib_name()

        # If path is to config file json, then get the path to parent dir
        if model_path.endswith(".json"):
            model_path = os.path.dirname(model_path)

        trained_model_paths[key] = model_path

    if "multi_instance" in trained_model_paths:
        predictor = BottomupPredictor.from_trained_models(
            trained_model_paths["multi_instance"]
        )
    elif "single_instance" in trained_model_paths:
        predictor = SingleInstancePredictor.from_trained_models(
            confmap_model_path=trained_model_paths["single_instance"]
        )
    elif (
        "centroid" in trained_model_paths and "centered_instance" in trained_model_paths
    ):
        predictor = TopdownPredictor.from_trained_models(
            centroid_model_path=trained_model_paths["centroid"],
            confmap_model_path=trained_model_paths["centered_instance"],
        )
    else:
        # TODO: support for tracking on previous predictions w/o model
        raise ValueError(
            f"Unable to run inference with {list(trained_model_paths.keys())} heads."
        )

    tracker = make_tracker_from_cli(args)
    predictor.tracker = tracker

    return predictor


def make_tracker_from_cli(args):
    policy_args = util.make_scoped_dictionary(vars(args), exclude_nones=True)

    tracker_name = "None"
    if "tracking" in policy_args:
        tracker_name = policy_args["tracking"].get("tracker", "None")

    if tracker_name.lower() != "none":
        tracker = Tracker.make_tracker_by_name(**policy_args["tracking"])
        return tracker

    return None


def save_predictions_from_cli(args, predicted_frames):
    from sleap import Labels

    if args.output:
        output_path = args.output
    else:
        out_dir = os.path.dirname(args.data_path)
        out_name = os.path.basename(args.data_path) + ".predictions.slp"
        output_path = os.path.join(out_dir, out_name)

    labels = Labels(labeled_frames=predicted_frames)

    print(f"Saving: {output_path}")
    Labels.save_file(labels, output_path)


def main():
    """CLI for running inference."""

    parser = make_cli_parser()
    args, _ = parser.parse_known_args()
    print(args)

    if args.cpu or not sleap.nn.system.is_gpu_system():
        sleap.nn.system.use_cpu_only()
    else:
        if args.first_gpu:
            sleap.nn.system.use_first_gpu()
        elif args.last_gpu:
            sleap.nn.system.use_last_gpu()
        else:
            sleap.nn.system.use_gpu(args.gpu)
    sleap.nn.system.disable_preallocation()

    print("System:")
    sleap.nn.system.summary()

    video_reader = make_video_reader_from_cli(args)
    print("Frames:", len(video_reader))

    predictor = make_predictor_from_cli(args)

    # Run inference!
    t0 = time.time()
    predicted_frames = predictor.predict(video_reader)

    save_predictions_from_cli(args, predicted_frames)
    print(f"Total Time: {time.time() - t0}")


if __name__ == "__main__":
    main()
