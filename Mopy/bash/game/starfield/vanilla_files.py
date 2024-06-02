# -*- coding: utf-8 -*-
#
# GPL License and Copyright Notice ============================================
#  This file is part of Wrye Bash.
#
#  Wrye Bash is free software: you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation, either version 3
#  of the License, or (at your option) any later version.
#
#  Wrye Bash is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with Wrye Bash.  If not, see <https://www.gnu.org/licenses/>.
#
#  Wrye Bash copyright (C) 2005-2009 Wrye, 2010-2024 Wrye Bash Team
#  https://github.com/wrye-bash
#
# =============================================================================
"""This module lists the files installed in the Data folder in a completely
vanilla Starfield setup."""
import os

vanilla_files = {f.replace('\\', os.sep) for f in {
    'BlueprintShips-Starfield - Localization.ba2',
    'BlueprintShips-Starfield.esm',
    'Constellation - Localization.ba2',
    'Constellation - Textures.ba2',
    'Constellation.esm',
    'data.bin',
    'OldMars - Localization.ba2',
    'OldMars - Textures.ba2',
    'OldMars.esm',
    'SFBGS006 - Main.ba2',
    'SFBGS006 - Textures.ba2',
    'SFBGS006.esm',
    'SFBGS007 - Main.ba2',
    'SFBGS007.esm',
    'SFBGS008 - Main.ba2',
    'SFBGS008.esm',
    'Starfield - Animations.ba2',
    'Starfield - DensityMaps.ba2',
    'Starfield - FaceAnimation01.ba2',
    'Starfield - FaceAnimation02.ba2',
    'Starfield - FaceAnimation03.ba2',
    'Starfield - FaceAnimation04.ba2',
    'Starfield - FaceAnimationPatch.ba2',
    'Starfield - FaceMeshes.ba2',
    'Starfield - GeneratedTextures.ba2',
    'Starfield - Interface.ba2',
    'Starfield - Localization.ba2',
    'Starfield - LODMeshes.ba2',
    'Starfield - LODMeshesPatch.ba2',
    'Starfield - LODTextures.ba2',
    'Starfield - LODTextures01.ba2',
    'Starfield - LODTextures02.ba2',
    'Starfield - Materials.ba2',
    'Starfield - Meshes01.ba2',
    'Starfield - Meshes02.ba2',
    'Starfield - MeshesPatch.ba2',
    'Starfield - Misc.ba2',
    'Starfield - Particles.ba2',
    'Starfield - ParticlesTestData.ba2',
    'Starfield - PlanetData.ba2',
    'Starfield - Shaders.ba2',
    'Starfield - ShadersBeta.ba2',
    'Starfield - Terrain01.ba2',
    'Starfield - Terrain02.ba2',
    'Starfield - Terrain03.ba2',
    'Starfield - Terrain04.ba2',
    'Starfield - TerrainPatch.ba2',
    'Starfield - Textures01.ba2',
    'Starfield - Textures02.ba2',
    'Starfield - Textures03.ba2',
    'Starfield - Textures04.ba2',
    'Starfield - Textures05.ba2',
    'Starfield - Textures06.ba2',
    'Starfield - Textures07.ba2',
    'Starfield - Textures08.ba2',
    'Starfield - Textures09.ba2',
    'Starfield - Textures10.ba2',
    'Starfield - Textures11.ba2',
    'Starfield - TexturesPatch.ba2',
    'Starfield - Voices01.ba2',
    'Starfield - Voices02.ba2',
    'Starfield - VoicesPatch.ba2',
    'Starfield - WwiseSounds01.ba2',
    'Starfield - WwiseSounds02.ba2',
    'Starfield - WwiseSounds03.ba2',
    'Starfield - WwiseSounds04.ba2',
    'Starfield - WwiseSounds05.ba2',
    'Starfield - WwiseSoundsPatch.ba2',
    'Starfield.esm',
    'video\\ArtifactVision01.bk2',
    'video\\ArtifactVision02.bk2',
    'video\\ArtifactVision03.bk2',
    'video\\ArtifactVision04.bk2',
    'video\\ArtifactVision05.bk2',
    'video\\ArtifactVision06.bk2',
    'video\\ArtifactVision07.bk2',
    'video\\ArtifactVision08.bk2',
    'video\\ArtifactVision09.bk2',
    'video\\BGS_LOGO_1080p_BinkVersion.bk2',
    'video\\EndingVision.bk2',
    'video\\MainMenuLoop.bk2',
    'video\\PowerVision_Alien.bk2',
    'video\\PowerVision_AntiGravField.bk2',
    'video\\PowerVision_CreateVac.bk2',
    'video\\PowerVision_CreatorsPeace.bk2',
    'video\\PowerVision_Earthbound.bk2',
    'video\\PowerVision_Elemental.bk2',
    'video\\PowerVision_Eternal.bk2',
    'video\\PowerVision_GravDash.bk2',
    'video\\PowerVision_GravityWave.bk2',
    'video\\PowerVision_GravityWell.bk2',
    'video\\PowerVision_InnerDemon.bk2',
    'video\\PowerVision_LifeForced.bk2',
    'video\\PowerVision_MoonForm.bk2',
    'video\\PowerVision_ParallelSelf.bk2',
    'video\\PowerVision_ParticleBeam.bk2',
    'video\\PowerVision_PersonalAtmosphere.bk2',
    'video\\PowerVision_PhasedTime.bk2',
    'video\\PowerVision_Precognition.bk2',
    'video\\PowerVision_ReactiveShield.bk2',
    'video\\PowerVision_SenseStar.bk2',
    'video\\PowerVision_SolarFlare.bk2',
    'video\\PowerVision_SunlessSpace.bk2',
    'video\\PowerVision_Supernova.bk2',
    'video\\PowerVision_VoidForm.bk2',
}}
