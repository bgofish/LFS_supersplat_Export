from __future__ import annotations

import unittest

from supersplat.lfs_export import LfsExportAdapter


class _Splat:
    def __init__(self, degree: int) -> None:
        self.degree = degree

    def active_sh_degree(self) -> int:
        return self.degree


class _Node:
    def __init__(self, node_id: int, name: str, children=(), splat=None, count=0) -> None:
        self.id = node_id
        self.name = name
        self.children = list(children)
        self._splat = splat
        self.gaussian_count = count

    def splat_data(self):
        return self._splat


class _Scene:
    def __init__(self, nodes, visible):
        self.nodes = nodes
        self.visible = visible

    def is_valid(self):
        return True

    def get_nodes(self):
        return self.nodes

    def get_visible_nodes(self):
        return self.visible


class _Lfs:
    def __init__(self, scene):
        self.scene = scene

    def get_scene(self):
        return self.scene


class LfsExportAdapterTests(unittest.TestCase):
    def test_all_scope_includes_splats_and_ignores_non_splats(self):
        leaf = _Node(2, "Leaf", splat=_Splat(2), count=10)
        group = _Node(1, "Group", children=(2,))
        camera = _Node(3, "Camera")
        scene = _Scene([group, leaf, camera], [leaf])
        adapter = LfsExportAdapter(_Lfs(scene), object())

        selection = adapter.resolve("all")

        self.assertEqual(selection.node_names, ["Leaf"])
        self.assertEqual(selection.gaussian_count, 10)
        self.assertEqual(selection.maximum_sh_degree, 2)

    def test_visible_scope_requires_a_splat(self):
        camera = _Node(1, "Camera")
        adapter = LfsExportAdapter(_Lfs(_Scene([camera], [camera])), object())

        with self.assertRaisesRegex(Exception, "contains no splat"):
            adapter.resolve("visible")


if __name__ == "__main__":
    unittest.main()
