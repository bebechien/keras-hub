import math

import keras
from keras import ops

from keras_hub.src.api_export import keras_hub_export
from keras_hub.src.utils.tensor_utils import assert_bounding_box_support

EPSILON = 1e-8


@keras_hub_export("keras_hub.layers.NonMaxSuppression")
class NonMaxSuppression(keras.layers.Layer):
    """A Keras layer that decodes predictions of an object detection model.

    Args:
        bounding_box_format: str. The format of bounding boxes of input dataset.
            Refer `keras.utils.bounding_boxes.convert_format` args for more
            details on supported bounding box formats.
        from_logits: boolean, True means input score is logits, False means
            confidence.
        iou_threshold: float. Value in the range [0, 1] representing the
            minimum IoU threshold for two boxes to be considered
            same for suppression. Defaults to 0.5.
        confidence_threshold: float. Value in the range [0, 1]. All boxes with
            confidence below this value will be discarded, defaults to 0.5.
        max_detections: int. the maximum detections to consider after nms is
            applied. A large number may trigger significant memory overhead,
            defaults to 100.

    Example:
    ```python
    boxes = np.random.uniform(low=0, high=1, size=(2, 5, 4))
    classes = np.expand_dims(
        np.array(
            [[0.1, 0.1, 0.4, 0.5, 0.9], [0.7, 0.5, 0.3, 0.0, 0.0]],
            "float32",
        ),
        axis=-1,
    )

    nms = keras_hub.layers.NonMaxSuppression(
        bounding_box_format="yxyx",
        from_logits=False,
        iou_threshold=1.0,
        confidence_threshold=0.1,
        max_detections=1,
    )

    nms_outputs = nms(boxes, classes)
    ```
    """

    def __init__(
        self,
        bounding_box_format,
        from_logits,
        iou_threshold=0.5,
        confidence_threshold=0.5,
        max_detections=100,
        **kwargs,
    ):
        # Check whether current version of keras support bounding box utils
        assert_bounding_box_support(self.__class__.__name__)
        super().__init__(**kwargs)
        self.bounding_box_format = bounding_box_format
        self.from_logits = from_logits
        self.iou_threshold = iou_threshold
        self.confidence_threshold = confidence_threshold
        self.max_detections = max_detections
        self.built = True

    def call(
        self,
        box_prediction,
        class_prediction,
        images=None,
    ):
        """Accepts images and raw scores, returning bounding box predictions.

        Args:
            box_prediction: Dense Tensor of shape [batch, boxes, 4] in the
                `bounding_box_format` specified in the constructor.
            class_prediction: Dense Tensor of shape [batch, boxes, num_classes].
        """
        target_format = "yxyx"
        height, width = None, None

        if "rel" in self.bounding_box_format and images is None:
            raise ValueError(
                "`images` cannot be None when using relative "
                "bounding box format."
            )

        if "rel" in self.bounding_box_format:
            target_format = "rel_" + target_format
            height, width, _ = ops.shape(images)

        box_prediction = keras.utils.bounding_boxes.convert_format(
            box_prediction,
            source=self.bounding_box_format,
            target=target_format,
            height=height,
            width=width,
        )
        if self.from_logits:
            class_prediction = ops.sigmoid(class_prediction)

        confidence_prediction = ops.max(class_prediction, axis=-1)

        idx, valid_det = non_max_suppression(
            box_prediction,
            confidence_prediction,
            max_output_size=self.max_detections,
            iou_threshold=self.iou_threshold,
            score_threshold=self.confidence_threshold,
        )

        box_prediction = ops.take_along_axis(
            box_prediction, ops.expand_dims(idx, axis=-1), axis=1
        )
        box_prediction = ops.reshape(
            box_prediction, (-1, self.max_detections, 4)
        )
        confidence_prediction = ops.take_along_axis(
            confidence_prediction, idx, axis=1
        )
        class_prediction = ops.take_along_axis(
            class_prediction, ops.expand_dims(idx, axis=-1), axis=1
        )

        box_prediction = keras.utils.bounding_boxes.convert_format(
            box_prediction,
            source=target_format,
            target=self.bounding_box_format,
            height=height,
            width=width,
        )
        bounding_boxes = {
            "boxes": box_prediction,
            "confidence": confidence_prediction,
            "labels": ops.argmax(class_prediction, axis=-1),
            "num_detections": valid_det,
        }

        # this is required to comply with bounding box format.
        return mask_invalid_detections(bounding_boxes)

    def get_config(self):
        config = super().get_config()
        config.update(
            {
                "bounding_box_format": self.bounding_box_format,
                "from_logits": self.from_logits,
                "iou_threshold": self.iou_threshold,
                "confidence_threshold": self.confidence_threshold,
                "max_detections": self.max_detections,
            }
        )
        return config


def non_max_suppression(
    boxes,
    scores,
    max_output_size,
    iou_threshold=0.5,
    score_threshold=0.0,
    tile_size=512,
):
    """Non-maximum suppression.

    Ported from https://github.com/tensorflow/tensorflow/blob/v2.12.0/tensorflow/python/ops/image_ops_impl.py#L5368-L5458

    Args:
        boxes: a tensor of rank 2 or higher with a shape of
            `[..., num_boxes, 4]`. Dimensions except the last two are batch
            dimensions. The last dimension represents box coordinates in
            yxyx format.
        scores: a tensor of rank 1 or higher with a shape of `[..., num_boxes]`.
            max_output_size: a scalar integer tensor representing the maximum
            number of boxes to be selected by non max suppression.
        iou_threshold: a float representing the threshold for
            deciding whether boxes overlap too much with respect
            to IoU (intersection over union).
        score_threshold: a float representing the threshold for box scores.
            Boxes with a score that is not larger than this threshold
            will be suppressed.
        tile_size: an integer representing the number of boxes in a tile, i.e.,
            the maximum number of boxes per image that can be used to suppress
            other boxes in parallel; larger tile_size means larger parallelism
            and potentially more redundant work.

    Returns:
        idx: a tensor with a shape of `[..., num_boxes]` representing the
            indices selected by non-max suppression. The leading dimensions
            are the batch dimensions of the input boxes. All numbers are within
            `[0, num_boxes)`. For each image (i.e., `idx[i]`), only the first
            `num_valid[i]` indices (i.e., `idx[i][:num_valid[i]]`) are valid.
        num_valid: a tensor of rank 0 or higher with a shape of [...]
            representing the number of valid indices in idx. Its dimensions
            are the batch dimensions of the input boxes.
    """

    def _sort_scores_and_boxes(scores, boxes):
        """Sort boxes based their score from highest to lowest.

        Args:
            scores: a tensor with a shape of `[batch_size, num_boxes]`
                representing the scores of boxes.
            boxes: a tensor with a shape of `[batch_size, num_boxes, 4]`
                representing the boxes.

        Returns:
            sorted_scores: a tensor with a shape of
                `[batch_size, num_boxes]` representing the sorted scores.
            sorted_boxes: a tensor representing the sorted boxes.
            sorted_scores_indices: a tensor with a shape of
                `[batch_size, num_boxes]` representing the index of the scores
                in a sorted descending order.
        """
        sorted_scores_indices = ops.flip(
            ops.cast(ops.argsort(scores, axis=1), "int32"), axis=1
        )
        sorted_scores = ops.take_along_axis(
            scores,
            sorted_scores_indices,
            axis=1,
        )
        sorted_boxes = ops.take_along_axis(
            boxes,
            ops.expand_dims(sorted_scores_indices, axis=-1),
            axis=1,
        )
        return sorted_scores, sorted_boxes, sorted_scores_indices

    batch_dims = ops.shape(boxes)[:-2]
    num_boxes = boxes.shape[-2]
    boxes = ops.reshape(boxes, [-1, num_boxes, 4])
    scores = ops.reshape(scores, [-1, num_boxes])
    batch_size = boxes.shape[0]
    if score_threshold != float("-inf"):
        score_mask = ops.cast(scores > score_threshold, scores.dtype)
        scores *= score_mask
        box_mask = ops.expand_dims(ops.cast(score_mask, boxes.dtype), 2)
        boxes *= box_mask

    scores, boxes, sorted_indices = _sort_scores_and_boxes(scores, boxes)

    pad = (
        math.ceil(max(num_boxes, max_output_size) / tile_size) * tile_size
        - num_boxes
    )
    boxes = ops.pad(ops.cast(boxes, "float32"), [[0, 0], [0, pad], [0, 0]])
    scores = ops.pad(ops.cast(scores, "float32"), [[0, 0], [0, pad]])
    num_boxes_after_padding = num_boxes + pad
    num_iterations = num_boxes_after_padding // tile_size

    def _loop_cond(unused_boxes, unused_threshold, output_size, idx):
        return ops.logical_and(
            ops.min(output_size) < ops.cast(max_output_size, "int32"),
            ops.cast(idx, "int32") < num_iterations,
        )

    def suppression_loop_body(boxes, iou_threshold, output_size, idx):
        return _suppression_loop_body(
            boxes, iou_threshold, output_size, idx, tile_size
        )

    selected_boxes, _, output_size, _ = ops.while_loop(
        _loop_cond,
        suppression_loop_body,
        [
            boxes,
            iou_threshold,
            ops.zeros([batch_size], "int32"),
            ops.array(0),
        ],
    )
    num_valid = ops.minimum(output_size, max_output_size)
    idx = num_boxes_after_padding - ops.cast(
        ops.top_k(
            ops.cast(ops.any(selected_boxes > 0, [2]), "int32")
            * ops.cast(
                ops.expand_dims(ops.arange(num_boxes_after_padding, 0, -1), 0),
                "int32",
            ),
            max_output_size,
        )[0],
        "int32",
    )
    idx = ops.minimum(idx, num_boxes - 1)

    index_offsets = ops.cast(ops.arange(batch_size) * num_boxes, "int32")
    take_along_axis_idx = ops.reshape(
        idx + ops.expand_dims(index_offsets, 1), [-1]
    )

    if keras.backend.backend() != "tensorflow":
        idx = ops.take_along_axis(
            ops.reshape(sorted_indices, [-1]), take_along_axis_idx
        )
    else:
        import tensorflow as tf

        idx = tf.gather(ops.reshape(sorted_indices, [-1]), take_along_axis_idx)
    idx = ops.reshape(idx, [batch_size, -1])

    invalid_index = ops.zeros([batch_size, max_output_size], dtype="int32")
    idx_index = ops.cast(
        ops.expand_dims(ops.arange(max_output_size), 0), "int32"
    )
    num_valid_expanded = ops.expand_dims(num_valid, 1)
    idx = ops.where(idx_index < num_valid_expanded, idx, invalid_index)

    num_valid = ops.reshape(num_valid, batch_dims)
    return idx, num_valid


def _bbox_overlap(boxes_a, boxes_b):
    """Calculates the overlap (iou - intersection over union) between boxes_a
        and boxes_b.

    Args:
        boxes_a: a tensor with a shape of `[batch_size, N, 4]`.
            `N` is the number of boxes per image. The last dimension is the
            pixel coordinates in `[ymin, xmin, ymax, xmax]` form.
      boxes_b: a tensor with a shape of `[batch_size, M, 4]`. M is the number of
        boxes. The last dimension is the pixel coordinates in
        `[ymin, xmin, ymax, xmax]` form.

    Returns:
        intersection_over_union: a tensor with as a shape of
            `[batch_size, N, M]`, representing the ratio of intersection area
            over union area (IoU) between two boxes
    """
    if len(boxes_a.shape) == 4:
        boxes_a = ops.squeeze(boxes_a, axis=0)
    a_y_min, a_x_min, a_y_max, a_x_max = ops.split(boxes_a, 4, axis=2)
    b_y_min, b_x_min, b_y_max, b_x_max = ops.split(boxes_b, 4, axis=2)

    # Calculates the intersection area.
    i_xmin = ops.maximum(a_x_min, ops.transpose(b_x_min, [0, 2, 1]))
    i_xmax = ops.minimum(a_x_max, ops.transpose(b_x_max, [0, 2, 1]))
    i_ymin = ops.maximum(a_y_min, ops.transpose(b_y_min, [0, 2, 1]))
    i_ymax = ops.minimum(a_y_max, ops.transpose(b_y_max, [0, 2, 1]))
    i_area = ops.maximum((i_xmax - i_xmin), 0) * ops.maximum(
        (i_ymax - i_ymin), 0
    )

    # Calculates the union area.
    a_area = (a_y_max - a_y_min) * (a_x_max - a_x_min)
    b_area = (b_y_max - b_y_min) * (b_x_max - b_x_min)

    # Adds a small epsilon to avoid divide-by-zero.
    u_area = a_area + ops.transpose(b_area, [0, 2, 1]) - i_area + EPSILON

    intersection_over_union = i_area / u_area

    return intersection_over_union


def _self_suppression(iou, _, iou_sum, iou_threshold):
    """Suppress boxes in the same tile.

    Compute boxes that cannot be suppressed by others (i.e.,
    can_suppress_others), and then use them to suppress boxes in the same tile.

    Args:
        iou: a tensor of shape `[batch_size, num_boxes_with_padding]`
            representing intersection over union.
        iou_sum: a scalar tensor.
        iou_threshold: a scalar tensor.

    Returns:
        iou_suppressed: a tensor of shape
            `[batch_size, num_boxes_with_padding]`.
        iou_diff: a scalar tensor representing whether any box is supressed in
            this step.
        iou_sum_new: a scalar tensor of shape `[batch_size]` that represents
            the iou sum after suppression.
        iou_threshold: a scalar tensor.
    """
    batch_size = ops.shape(iou)[0]
    can_suppress_others = ops.cast(
        ops.reshape(ops.max(iou, 1) < iou_threshold, [batch_size, -1, 1]),
        iou.dtype,
    )
    iou_after_suppression = (
        ops.reshape(
            ops.cast(
                ops.max(can_suppress_others * iou, 1) < iou_threshold, iou.dtype
            ),
            [batch_size, -1, 1],
        )
        * iou
    )
    iou_sum_new = ops.sum(iou_after_suppression, [1, 2])
    return [
        iou_after_suppression,
        ops.any(iou_sum - iou_sum_new > iou_threshold),
        iou_sum_new,
        iou_threshold,
    ]


def _cross_suppression(boxes, box_slice, iou_threshold, inner_idx, tile_size):
    """Suppress boxes between different tiles.

    Args:
        boxes: a tensor of shape `[batch_size, num_boxes_with_padding, 4]`
        box_slice: a tensor of shape `[batch_size, tile_size, 4]`
        iou_threshold: a scalar tensor
        inner_idx: a scalar tensor representing the tile index of the tile
            that is used to supress box_slice
        tile_size: an integer representing the number of boxes in a tile

    Returns:
        boxes: unchanged boxes as input
        box_slice_after_suppression: box_slice after suppression
        iou_threshold: unchanged
    """
    slice_index = ops.expand_dims(
        ops.expand_dims(
            ops.cast(
                ops.linspace(
                    inner_idx * tile_size,
                    (inner_idx + 1) * tile_size - 1,
                    tile_size,
                ),
                "int32",
            ),
            axis=0,
        ),
        axis=-1,
    )
    new_slice = ops.expand_dims(
        ops.take_along_axis(boxes, slice_index, axis=1), 0
    )
    iou = _bbox_overlap(new_slice, box_slice)
    box_slice_after_suppression = (
        ops.expand_dims(
            ops.cast(ops.all(iou < iou_threshold, [1]), box_slice.dtype), 2
        )
        * box_slice
    )
    return boxes, box_slice_after_suppression, iou_threshold, inner_idx + 1


def _suppression_loop_body(boxes, iou_threshold, output_size, idx, tile_size):
    """Process boxes in the range [idx*tile_size, (idx+1)*tile_size).

    Args:
        boxes: a tensor with a shape of [batch_size, anchors, 4].
        iou_threshold: a float representing the threshold for deciding whether
            boxes overlap too much with respect to IOU.
        output_size: an int32 tensor of size [batch_size]. Representing the
            number of selected boxes for each batch.
        idx: an integer scalar representing induction variable.
        tile_size: an integer representing the number of boxes in a tile

    Returns:
        boxes: updated boxes.
        iou_threshold: pass down iou_threshold to the next iteration.
        output_size: the updated output_size.
        idx: the updated induction variable.
    """
    num_tiles = boxes.shape[1] // tile_size
    batch_size = boxes.shape[0]

    def cross_suppression_func(boxes, box_slice, iou_threshold, inner_idx):
        return _cross_suppression(
            boxes, box_slice, iou_threshold, inner_idx, tile_size
        )

    # Iterates over tiles that can possibly suppress the current tile.
    slice_index = ops.expand_dims(
        ops.expand_dims(
            ops.cast(
                ops.linspace(
                    idx * tile_size, (idx + 1) * tile_size - 1, tile_size
                ),
                "int32",
            ),
            axis=0,
        ),
        axis=-1,
    )
    box_slice = ops.take_along_axis(boxes, slice_index, axis=1)
    _, box_slice, _, _ = ops.while_loop(
        lambda _boxes, _box_slice, _threshold, inner_idx: inner_idx < idx,
        cross_suppression_func,
        [boxes, box_slice, iou_threshold, ops.array(0)],
    )

    # Iterates over the current tile to compute self-suppression.
    iou = _bbox_overlap(box_slice, box_slice)
    mask = ops.expand_dims(
        ops.reshape(ops.arange(tile_size), [1, -1])
        > ops.reshape(ops.arange(tile_size), [-1, 1]),
        0,
    )
    iou *= ops.cast(ops.logical_and(mask, iou >= iou_threshold), iou.dtype)
    suppressed_iou, _, _, _ = ops.while_loop(
        lambda _iou, loop_condition, _iou_sum, _: loop_condition,
        _self_suppression,
        [iou, ops.array(True), ops.sum(iou, [1, 2]), iou_threshold],
    )
    suppressed_box = ops.sum(suppressed_iou, 1) > 0
    box_slice *= ops.expand_dims(
        1.0 - ops.cast(suppressed_box, box_slice.dtype), 2
    )

    # Uses box_slice to update the input boxes.
    mask = ops.reshape(
        ops.cast(ops.equal(ops.arange(num_tiles), idx), boxes.dtype),
        [1, -1, 1, 1],
    )
    boxes = ops.tile(
        ops.expand_dims(box_slice, 1), [1, num_tiles, 1, 1]
    ) * mask + ops.reshape(boxes, [batch_size, num_tiles, tile_size, 4]) * (
        1 - mask
    )
    boxes = ops.reshape(boxes, [batch_size, -1, 4])

    # Updates output_size.
    output_size += ops.cast(ops.sum(ops.any(box_slice > 0, [2]), [1]), "int32")
    return boxes, iou_threshold, output_size, idx + 1


def mask_invalid_detections(bounding_boxes):
    """masks out invalid detections with -1s.

    This utility is mainly used on the output of non-max suppression operations.
    The output of non-max-suppression contains all the detections, even invalid
    ones. Users are expected to use `num_detections` to determine how many boxes
    are in each image.

    In contrast, KerasHub expects all bounding boxes to be padded with -1s.
    This function uses the value of `num_detections` to mask out
    invalid boxes with -1s.

    Args:
        bounding_boxes: a dictionary complying with Keras bounding box format.
            In addition to the normal required keys, these boxes are also
            expected to have a `num_detections` key.

    Returns:
        bounding boxes with proper masking of the boxes according to
        `num_detections`. This allows proper interop with non-max supression.
        Returned boxes match the specification fed to the function, so if the
        bounding box tensor uses `tf.RaggedTensor` to represent boxes the
        returned value will also return `tf.RaggedTensor` representations.
    """
    # ensure we are complying with Keras bounding box format.
    if (
        not isinstance(bounding_boxes, dict)
        or "labels" not in bounding_boxes
        or "boxes" not in bounding_boxes
    ):
        raise ValueError(
            "Expected `bounding_boxes` agurment to be a "
            "dict with keys 'boxes' and 'labels'. Received: "
            f"bounding_boxes={bounding_boxes}"
        )

    if "num_detections" not in bounding_boxes:
        raise ValueError(
            "`bounding_boxes` must have key 'num_detections' "
            "to be used with `mask_invalid_detections()`."
        )

    boxes = bounding_boxes.get("boxes")
    labels = bounding_boxes.get("labels")
    if isinstance(boxes, list):
        if not isinstance(labels, list):
            raise ValueError(
                "If `bounding_boxes['boxes']` is a list, then "
                "`bounding_boxes['labels']` must also be a list."
                f"Received: bounding_boxes['labels']={labels}"
            )
        if len(boxes) != len(labels):
            raise ValueError(
                "If `bounding_boxes['boxes']` and "
                "`bounding_boxes['labels']` are both lists, "
                "they must have the same length. Received: "
                f"len(bounding_boxes['boxes'])={len(boxes)} and "
                f"len(bounding_boxes['labels'])={len(labels)} and "
            )
    confidence = bounding_boxes.get("confidence", None)
    num_detections = bounding_boxes.get("num_detections")

    # Create a mask to select only the first N boxes from each batch
    mask = ops.cast(
        ops.expand_dims(ops.arange(boxes.shape[1]), axis=0),
        num_detections.dtype,
    )
    mask = mask < num_detections[:, None]

    labels = ops.where(mask, labels, -ops.ones_like(labels))

    if confidence is not None:
        confidence = ops.where(mask, confidence, -ops.ones_like(confidence))

    # reuse mask for boxes
    mask = ops.expand_dims(mask, axis=-1)
    mask = ops.repeat(mask, repeats=boxes.shape[-1], axis=-1)
    boxes = ops.where(mask, boxes, -ops.ones_like(boxes))

    result = bounding_boxes.copy()

    result["boxes"] = boxes
    result["labels"] = labels
    if confidence is not None:
        result["confidence"] = confidence

    return result
