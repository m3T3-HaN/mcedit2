"""
    ${NAME}
"""
from __future__ import absolute_import, division, print_function, unicode_literals
import logging
import sys
import collections
import numpy

from mcedit2.rendering.layers import Layer
from mcedit2.rendering import chunkupdate, scenegraph
from mcedit2.rendering import renderstates
from mcedit2.rendering.chunknode import ChunkNode, ChunkRenderInfo, ChunkGroupNode
from mcedit2.rendering.depths import DepthOffset
from mcedit2.rendering.geometrycache import GeometryCache

log = logging.getLogger(__name__)



def layerProperty(layer, default=True):
    attr = intern(str("_draw" + layer))

    def _get(self):
        return getattr(self, attr, default)

    def _set(self, val):
        if val != _get(self):
            setattr(self, attr, val)
            self.toggleLayer(val, layer)

    return property(_get, _set)

DEBUG_WORLDMESH_LISTS = "-debuglists" in sys.argv

class SceneUpdateTask(object):
    showRedraw = True
    showHiddenOres = False
    showChunkRedraw = True

    spaceHeight = 64
    targetFPS = 30

    def __init__(self, worldScene, textureAtlas, bounds=None):
        """

        :type worldScene: WorldScene
        :type bounds: BoundingBox
        :type textureAtlas: TextureAtlas
        """
        self.worldScene = worldScene

        self.render = True
        self.rotation = 0

        self.alpha = 255

        self.textureAtlas = textureAtlas

        self.renderType = numpy.zeros((256*256,), 'uint8')
        self.renderType[:] = 3
        for block in self.worldScene.dimension.blocktypes:
            self.renderType[block.ID] = block.renderType

    overheadMode = False

    maxWorkFactor = 64
    minWorkFactor = 1
    workFactor = 2


    def wantsChunk(self, cPos):
        chunkInfo = self.worldScene.chunkRenderInfo.get(cPos)
        if chunkInfo is None:
            return True

        #log.info("Wants chunk %s? %s", cPos, len(chunkNode.invalidLayers))
        return len(chunkInfo.invalidLayers)

    def workOnChunk(self, chunk, visibleSections=None):
        work = 0
        cPos = chunk.chunkPosition

        chunkInfo = self.worldScene.getChunkRenderInfo(cPos)

        chunkInfo.visibleSections = visibleSections
        try:
            chunkUpdate = chunkupdate.ChunkUpdate(self, chunkInfo, chunk)
            for _ in chunkUpdate:
                work += 1
                if (work % SceneUpdateTask.workFactor) == 0:
                    yield

            chunkInfo.invalidLayers = set()
            meshesByRS = collections.defaultdict(list)
            for mesh in chunkUpdate.blockMeshes:
                meshesByRS[mesh.renderstate].append(mesh)

            for renderstate in renderstates.allRenderstates:
                groupNode = self.worldScene.getRenderstateGroup(renderstate)
                meshes = meshesByRS[renderstate]
                if len(meshes):
                    arrays = sum([mesh.vertexArrays for mesh in meshes], [])
                    if len(arrays):
                        chunkNode = ChunkNode(cPos)
                        groupNode.addChunkNode(chunkNode)
                        node = scenegraph.VertexNode(arrays)
                        chunkNode.addChild(node)
                    else:
                        groupNode.discardChunkNode(*cPos)

                        # Need a way to signal WorldScene that this chunk didn't need updating in this renderstate,
                        # but it should keep the old vertex arrays for the state.
                        # Alternately, signal WorldScene that the chunk did update for the renderstate and no
                        # vertex arrays resulted. Return a mesh with zero length vertexArrays
                        #else:
                        #    groupNode.discardChunkNode(*cPos)


        except Exception as e:
            logging.exception(u"Rendering chunk %s failed: %r", cPos, e)


class WorldScene(scenegraph.Node):
    def __init__(self, dimension, textureAtlas, geometryCache=None, bounds=None):
        super(WorldScene, self).__init__()

        self.dimension = dimension
        self.textureAtlas = textureAtlas
        self.depthOffsetNode = scenegraph.DepthOffsetNode(DepthOffset.Renderer)
        self.addChild(self.depthOffsetNode)

        self.textureAtlasNode = scenegraph.TextureAtlasNode(textureAtlas)
        self.depthOffsetNode.addChild(self.textureAtlasNode)

        self.renderstateNodes = {}
        for rsClass in renderstates.allRenderstates:
            rsNode = scenegraph.RenderstateNode(rsClass)
            self.textureAtlasNode.addChild(rsNode)
            self.renderstateNodes[rsClass] = rsNode

        self.groupNodes = {}  # by renderstate
        self.chunkRenderInfo = {}
        self.visibleLayers = set(Layer.AllLayers)

        self.updateTask = SceneUpdateTask(self, textureAtlas, bounds)

        if geometryCache is None:
            geometryCache = GeometryCache()
        self.geometryCache = geometryCache

        self.showRedraw = False

        self.minlod = 0
        self.bounds = bounds

    def chunkPositions(self):
        return self.chunkRenderInfo.iterkeys()

    def getRenderstateGroup(self, rsClass):
        groupNode = self.groupNodes.get(rsClass)
        if groupNode is None:
            groupNode = ChunkGroupNode()
            self.groupNodes[rsClass] = groupNode
            self.renderstateNodes[rsClass].addChild(groupNode)

        return groupNode
    #
    # def toggleLayer(self, val, layer):
    #     self.chunkGroupNode.toggleLayer(val, layer)
    #
    # drawEntities = layerProperty(Layer.Entities)
    # drawTileEntities = layerProperty(Layer.TileEntities)
    # drawTileTicks = layerProperty(Layer.TileTicks)
    # drawMonsters = layerProperty(Layer.Monsters)
    # drawItems = layerProperty(Layer.Items)
    # drawTerrainPopulated = layerProperty(Layer.TerrainPopulated)

    def discardChunk(self, cx, cz):
        """
        Discard the chunk at the given position from the scene
        """
        for groupNode in self.groupNodes.itervalues():
            groupNode.discardChunkNode(cx, cz)
        self.chunkRenderInfo.pop((cx, cz), None)

    def discardChunks(self, chunks):
        for cx, cz in chunks:
            self.discardChunk(cx, cz)

    def discardAllChunks(self):
        for groupNode in self.groupNodes.itervalues():
            groupNode.clear()
        self.chunkRenderInfo.clear()

    def invalidateChunk(self, cx, cz, invalidLayers=None):
        """
        Mark the chunk for regenerating vertex data
        """
        node = self.chunkRenderInfo.get((cx, cz))
        if node:
            node.invalidLayers = invalidLayers or Layer.AllLayers

    _fastLeaves = False

    @property
    def fastLeaves(self):
        return self._fastLeaves

    @fastLeaves.setter
    def fastLeaves(self, val):
        if self._fastLeaves != bool(val):
            self.discardAllChunks()

        self._fastLeaves = bool(val)

    _roughGraphics = False

    @property
    def roughGraphics(self):
        return self._roughGraphics

    @roughGraphics.setter
    def roughGraphics(self, val):
        if self._roughGraphics != bool(val):
            self.discardAllChunks()

        self._roughGraphics = bool(val)

    _showHiddenOres = False

    @property
    def showHiddenOres(self):
        return self._showHiddenOres

    @showHiddenOres.setter
    def showHiddenOres(self, val):
        if self._showHiddenOres != bool(val):
            self.discardAllChunks()

        self._showHiddenOres = bool(val)

    def wantsChunk(self, cPos):
        return self.updateTask.wantsChunk(cPos)

    def workOnChunk(self, chunk, visibleSections=None):
        return self.updateTask.workOnChunk(chunk, visibleSections)

    def getChunkRenderInfo(self, cPos):
        chunkInfo = self.chunkRenderInfo.get(cPos)
        if chunkInfo is None:
            #log.info("Creating ChunkRenderInfo %s in %s", cPos, self.worldScene)
            chunkInfo = ChunkRenderInfo(self, cPos)
            self.chunkRenderInfo[cPos] = chunkInfo

        return chunkInfo
