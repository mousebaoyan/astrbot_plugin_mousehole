import numpy as np

from tools.duplicates import groups


def test_threads_alike_through_another_are_one_group_the_most_alike_first():
    vectors = np.array(
        [[1, 0, 0], [0.99, 0.14, 0], [0.97, 0.24, 0], [0, 1, 0], [0, 0, 1], [0, 0.05, 1]],
        dtype=np.float32,
    )
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    # 10 and 12 are less alike than the bar, but each is like 11.
    found = groups(np.array([10, 11, 12, 13, 14, 15]), vectors, 0.98)
    assert [g for _, g in found] == [[14, 15], [10, 11, 12]]
    assert found[0][0] > found[1][0] >= 0.98
