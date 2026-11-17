extends Node3D

const SHADER_PATH := "res://shaders/weapon_clip_and_fov_shader.gdshader"

@onready var view_model_container: Node3D = $ViewModel

## Apply the weapon clip shader to every mesh inside the view model hierarchy.
func apply_clip_and_fov_shader_to_view_model(node3d: Node3D, fov_or_negative_for_unchanged := -1.0) -> void:
	var all_mesh_instances = node3d.find_children("*", "MeshInstance3D")
	if node3d is MeshInstance3D:
		all_mesh_instances.push_back(node3d)

	for mesh_instance in all_mesh_instances:
		var mesh = mesh_instance.mesh
		if mesh == null:
			continue
		mesh_instance.cast_shadow = GeometryInstance3D.SHADOW_CASTING_SETTING_OFF
		for surface_idx in mesh.get_surface_count():
			var base_mat = mesh.surface_get_material(surface_idx)
			if not base_mat or not (base_mat is BaseMaterial3D):
				continue
			var weapon_shader_material := ShaderMaterial.new()
			weapon_shader_material.shader = preload(SHADER_PATH)
			weapon_shader_material.set_shader_parameter("texture_albedo", base_mat.albedo_texture)
			weapon_shader_material.set_shader_parameter("texture_metallic", base_mat.metallic_texture)
			weapon_shader_material.set_shader_parameter("texture_roughness", base_mat.roughness_texture)
			weapon_shader_material.set_shader_parameter("texture_normal", base_mat.normal_texture)
			weapon_shader_material.set_shader_parameter("albedo", base_mat.albedo_color)
			weapon_shader_material.set_shader_parameter("metallic", base_mat.metallic)
			weapon_shader_material.set_shader_parameter("specular", base_mat.metallic_specular)
			weapon_shader_material.set_shader_parameter("roughness", base_mat.roughness)
			weapon_shader_material.set_shader_parameter("viewmodel_fov", fov_or_negative_for_unchanged)
			var tex_channels = {
				0: Vector4(1., 0., 0., 0.),
				1: Vector4(0., 1., 0., 0.),
				2: Vector4(0., 0., 1., 0.),
				3: Vector4(1., 0., 0., 1.),
				4: Vector4()
			}
			weapon_shader_material.set_shader_parameter("metallic_texture_channel", tex_channels[base_mat.metallic_texture_channel])
			mesh.surface_set_material(surface_idx, weapon_shader_material)
