import numpy as np
import tensorflow as tf
import dnnlib
import dnnlib.tflib as tflib
from tqdm import tqdm, trange
from training import misc

import PIL.Image
import glob

# Compute the effective batch size given a total number of elements, the batch index, and the 
# batch size. For the last batch, its effective size might be smaller if the total_num is not 
# evenly divided by the batch_size.
def curr_batch_size(total_num, idx, batch_size):
    start = idx * batch_size
    end = min((idx + 1) * batch_size, total_num)
    return end - start

# Mathematical utilities 
# ----------------------------------------------------------------------------

# Broadcast the latent across all layers
def broadcastLtnt(Gs, ltnt):
    return np.tile(ltnt[:, :, np.newaxis], [1, 1, Gs.components.synthesis.input_shape[2], 1])

# Linear interpolation
def lerp(a, b, ts):
    ts = ts[:, np.newaxis]
    return a + (b - a) * ts

def normalize(v):
    return v / np.linalg.norm(v, axis = -1, keepdims = True)

# Spherical interpolation
def slerp(a, b, ts):
    ts = ts[:, np.newaxis]
    a = normalize(a)
    b = normalize(b)
    d = np.sum(a * b, axis = -1, keepdims = True)
    p = ts * np.arccos(d)
    c = normalize(b - d * a)
    d = a * np.cos(p) + c * np.sin(p)
    return normalize(d)

# Evaluate: generate variety of samples
# ----------------------------------------------------------------------------

## Creates variety of visualizations. Supported types
# imgs           : Save image samples
# ltnts          : Save latent vectors
# maps           : Save attention maps (for GANsformer only)
# layer_maps     : Save attention maps for all layers (for GANsformer only)
# interpolations : Create latent interpolations
# noise_var      : Create noise variation visualization
# style_mix      : Create style mixing visualization
def eval(G, 
    dataset,                          # The dataset object for accessing the data
    batch_size,                       # Visualization batch size
    training            = False,      # Training mode
    latents             = None,       # Source latents to generate images from
    labels              = None,       # Source labels to generate images from (0 if no labels are used)
    # Model settings
    component_num,                    # Number of components the model has
    drange_net          = [-1,1],     # Model image output range
    # Visualization settings
    vis_types           = None,       # Visualization types to be created
    num                 = 100,        # Number of produced samples
    rich_num            = 5,          # Number of samples for which richer visualizations will be created 
                                      # (requires more memory and disk space, and therefore rich_num < num)
    grid                = None,       # Whether to save the samples in one large grid files 
                                      # or in separated files one per sample
    step                = None,       # Step number to be used in visualization filenames
    # Visualization-specific settings
    alpha               = 0.3,        # Proportion for generated images and attention maps blends
    intrp_density       = 8,          # Number of samples in between two end points of an interpolation
    intrp_per_component = False,      # Whether to perform interpolation along particular latent components (True) 
                                      # or all of them at once (False)
    noise_samples_num   = 100,        # Number of samples used to compute noise variation visualization
    section_size        = 100):       # Visualization section size (section_size <= num) for reducing memory footprint

    pattern_of = lambda dir, step, suffix: "eval/{}/{}%06d.{}".format(dir, "" if step is None else (step + "_"), suffix)

    # For time efficiency, during training save only image and map samples 
    # rather than richer visualizations
    vis = vis_types 
    if training:
        vis = {"imgs", "maps"}
        section_size = num = len(latents)
    else:
        if vis is None:
            vis = {"imgs", "maps", "ltnts", "interpolations", "noise_var"}
            
    # By default saves image samples in one grid file during training
    if grid is None: 
        grid = training

    # build image functions
    save_images = save_imgs_builder(drange_net, grid)
    save_blends = save_blends_builder(drange_net, grid, alpha)

    # Set up logging    
    grid_latents = np.random.randn(np.prod(grid_size), *G.input_shape[1:])
    noise_vars = [var for name, var in Gs.components.synthesis.vars.items() if name.startswith("noise")]
    noise_var_vals = {var: np.random.randn(*var.shape.as_list()) for var in noise_vars}
    tflib.set_vars(noise_var_vals)

    # Create directories
    dirs = []
    if save_imgs:            dirs += ["images"]
    if save_ltnts:           dirs += ["latents-z", "latents-w"]
    if save_maps:            dirs += ["maps", "softmaps", "blends", "softblends"]    
    if save_layer_maps:      dirs += ["layer_maps"]        
    if save_interpolations:  dirs += ["interpolations-z", "interpolation-w"]

    for dir in dirs:
        misc.mkdir(dnnlib.make_run_dir_path("eval/{}".format(dir)))

    # Produce visualizations
    for idx in range(0, num, section_size):
        curr_size = curr_batch_size(num, idx, section_size)
        if num > curr_size:
            print("--- Batch {}/{}".format(idx + 1, num))

        # Compute source latents images will be produced from
        if latents is None:
            latents = np.random.randn(curr_size, *G.input_shape[1:])
        if labels is None:
            labels = dataset.get_minibatch_np(curr_size)

        # Run network over latents and produce images and attention maps
        print("Running network...")
        images, attmaps_all_layers, wlatents_all_layers = G.run(latents, labels, randomize_noise = False, 
            is_visualization = True, minibatch_size = batch_size, return_dlatents = True)
        # For memory efficiency, save full information only for a small amount of images
        attmaps_all_layers = attmaps_all_layers[:rich_num]
        wlatents = wlatents_layers[:,:,0]

        # Save image samples
        if save_imgs:
            print("Saving image samples...")
            save_images(images, pattern_of("images", step, "png"), idx)

        # Save latent vectors
        if save_ltnts:
            print("Saving latents...")
            misc.save_npys(latents, pattern_of("latents-z", step, "npy"), idx)
            misc.save_npys(wlatents, pattern_of("latents-w", step, "npy"), idx) 

        # For the GANsformer model, save attention maps
        if attention:
            if save_maps:
                soft_maps = attmaps_all_layers[:,:,-1,0]

                pallete = np.expand_dims(get_colors(component_num), axis = [2, 3])
                maps = (soft_maps == np.amax(soft_maps, axis = 1, keepdims = True)).astype(float)

                soft_maps = np.sum(pallete * np.expand_dims(soft_maps, axis = 2), axis = 1)
                maps = np.sum(pallete * np.expand_dims(maps, axis = 2), axis = 1)

                print("Saving maps...")
                save_images(soft_maps, pattern_of("softmaps", step, "png"), idx)
                save_images(maps, pattern_of("maps", step, "png"), idx)

                save_blends(maps, images, pattern_of("softblends", step, "png"), idx)
                save_blends(soft_maps, images, pattern_of("blends", step, "png"), idx)

            # Save maps from all attention heads and layers 
            # (for efficiency, only for a small number of images)
            if save_layer_maps:
                all_maps = []
                maps_fakes = np.split(attmaps_all_layers, attmaps_all_layers.shape[2], axis = 2)
                for layer, lmaps in enumerate(maps_fakes):
                    lmaps = np.split(np.squeeze(lmaps, axis = 2), mapfakes.shape[3], axis = 2)
                    for head, hmap in enumerate(lmaps):
                        hmap = (hmap == np.amax(hmap, axis = 1, keepdims = True)).astype(float)
                        hmap = np.sum(pallete * hmap, axis = 1)
                        all_maps.append((hmap, "l{}_h{}".format(layer, head)))

                print("Saving layer maps...")
                for i in trange(rich_num):
                    misc.mkdir(dnnlib.make_run_dir_path("eval/layer_maps/%06d" % i))    

                for maps, name in tqdm(all_maps):
                    dirname = "eval/layer_maps{}/%06d/{}{}.png".format("" if step is None else ("/" + step), name)
                    save_images(maps, dirname, idx)

    # Produce interpolations between pairs or source latents
    # In the GANsformer case, varying one component at a time
    if save_interpolations:
        ts = np.array(np.linspace(0.0, 1.0, num = intrp_density, endpoint = True))

        print("Generating interpolations...")
        for i in trange(rich_num):
            misc.mkdir(dnnlib.make_run_dir_path("eval/interpolations-z/%06d" % i))                
            misc.mkdir(dnnlib.make_run_dir_path("eval/interpolations-w/%06d" % i))                

            z = np.random.randn(2, *G.input_shape[1:])
            z[0] = latents[i:i+1]
            w = G.run(z, labels, randomize_noise = False, return_dlatents = True, 
                minibatch_size = batch_size)[-1]

            def update(t, fn, ts, dim):
                if dim == 3: 
                    ts = ts[:, np.newaxis]
                t_ups = []

                if intrp_per_component:
                    for c in range(component_num):
                        # copy over all the components except component c that will get interpolated
                        t_up = np.tile(np.copy(t[0])[None], [intrp_density] + [1] * dim)
                        # interpolate component c
                        t_up[:,c] = fn(t[0, c], t[1, c], ts)
                        t_ups.append(t_up)
                    
                    t_up = np.concatenate(t_ups, axis = 0)
                else:
                    t_up = fn(t[0], t[1], ts)
                    
                return t_up

            z_up = update(z, slerp, ts, 2)
            w_up = update(w, lerp, ts, 3)

            imgs1 = G.run(z_up, labels, randomize_noise = False, minibatch_size = batch_size)[0]
            imgs2 = G.run(w_up, labels, randomize_noise = False, minibatch_size = batch_size, 
                take_wlatents = True)[0]

            def save_interpolation(imgs, name):
                imgs = np.split(imgs, component_num, axis = 0)
                for c in range(component_num):
                    filename = "eval/interpolations_%s/%06d/%02d" % (name, i, c)
                    imgs[c] = [misc.to_pil(img, drange = drange_net) for img in imgs[c]]
                    imgs[c][-1].save(dnnlib.make_run_dir_path("{}.png".format(filename)))
                    misc.save_gif(imgs[c], dnnlib.make_run_dir_path("{}.gif".format(filename))) 

            save_interpolation(imgs1, "z")
            save_interpolation(imgs2, "w")

    # Compute noise variance map
    # Shows what areas vary the most given fixed source 
    # latents due to the use of stochastic local noise
    if save_noise_var:
        print("Generating noise variance...")
        z = np.tile(np.random.randn(1, *G.input_shape[1:]), [noise_samples_num, 1, 1])
        imgs = G.run(z, labels, minibatch_size = batch_size)[0] 
        imgs = np.stack([misc.to_pil(img, drange = drange_net) for img in imgs], axis = 0)
        diff = np.std(np.mean(imgs, axis = 3), axis = 0) * 4
        diff = np.clip(diff + 0.5, 0, 255).astype(np.uint8)            
        PIL.Image.fromarray(diff, "L").save(dnnlib.make_run_dir_path("eval/noise_variance.png"))

    # Compute style mixing table, varying using the latent A in some of the layers and latent B in rest.
    # For the GANsformer, also produce component mixes (using latents from A in some of the components, 
    # and latents from B in the rest.
    if save_style_mix:
        print("Generating style mixes...")
        cols, rows = 4, 2
        row_lens = np.array([2, 5, 8, 11])

        # Create latent mixes
        mixes = {
            "layer": (np.arange(wlatents_layers.shape[2]) < row_lens[:,None]).astype(np.float32)[:,None,None,None,:,None]
            "component": (np.arange(wlatents_layers.shape[1]) < row_lens[:,None]).astype(np.float32)[:,None,None,:,None,None]
        }
        ws = wlatents_layers[:cols+rows]
        orig_imgs = images[:cols+rows]
        col_ltnts = wlatents_layers[:cols][None, None]
        row_ltnts = wlatents_layers[cols:cols+rows][None,:,None]
        
        for name, mix in mixes.items():
            # Produce image mixes
            mix_ltnts = mix * row_ltnts  + (1 - mix) * col_ltnts
            mix_ltnts = np.reshape(mix_ltnts, [-1, *wlatents_layers.shape[1:]])
            mix_imgs = G.run(mix_ltnts, labels, randomize_noise = False, take_dlatents = True,
                minibatch_size = batch_size)[0]
            mix_imgs = np.reshape(mix_imgs, [len(row_lens) * rows, cols, *mix_imgs.shape[1:]])

            # Create image table canvas
            H, W = mix_imgs.shape[-2:]
            canvas = PIL.Image.new("RGB", (W * (cols + 1), H * (len(row_lens) * rows + 1)), "black")
            
            # Place image mixes respectively at each position (row_idx, col_idx)
            for row_idx, row_elem in enumerate([None] + list(range(len(row_lens) * rows))):
                for col_idx, col_elem in enumerate([None] + list(range(cols))):
                    if row_elem, col_elem == (None, None):  continue
                    if row_elem is None:                    img = orig_imgs[col_elem]
                    elif col_elem is None:                  img = orig_imgs[cols + (row_elem % rows)]
                    else:                                   img =  mix_imgs[row_elem, col_elem]

                    canvas.paste(misc.to_pil(img, drange = drange_net), (W * col_idx, H * row_idx))
            
            canvas.save(dnnlib.make_run_dir_path("eval/{}_mixing.png".format(name)))

    print(misc.bcolored("Visualizations Completed!", "blue"))
