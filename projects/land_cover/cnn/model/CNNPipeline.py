# -*- coding: utf-8 -*-

import os
import sys
import glob
import logging

import xarray as xr
import numpy as np
from tifffile import imsave
from tifffile import imread
from sklearn.feature_extraction import image

import torch
from torch import nn
import rasterio as rio
from torch.utils.data import Dataset, DataLoader, sampler

from pytorch_toolbelt.inference.tiles import ImageSlicer, TileMerger
from pytorch_toolbelt.utils.torch_utils import to_numpy

from .UNet import UNet
from .Config import Config
from .Loss import FocalLoss, mIoULoss

import cv2
from scipy.ndimage import median_filter, binary_fill_holes

class CloudDataset(Dataset):

    def __init__(self, dataset_dir, pytorch=True):

        super().__init__()

        self.files = self.list_files(dataset_dir)
        self.pytorch = pytorch

    # -------------------------------------------------------------------------
    # Common methods
    # -------------------------------------------------------------------------
    def __len__(self):
        return len(self.files)

    def __repr__(self):
        s = 'Dataset class with {} files'.format(self.__len__())
        return s

    def __getitem__(self, idx, augment: bool = True):

        # get data
        x = torch.tensor(self.open_image(idx), dtype=torch.float32)
        y = torch.tensor(self.open_mask(idx), dtype=torch.torch.int64)

        # augment the data
        if augment:

            if np.random.random_sample() > 0.5:  # flip left and right
                x = torch.fliplr(x)
                y = torch.fliplr(y)
            if np.random.random_sample() > 0.5:  # reverse second dimension
                x = torch.flipud(x)
                y = torch.flipud(y)
            if np.random.random_sample() > 0.5:  # rotate 90 degrees
                x = torch.rot90(x, k=1, dims=[1, 2])
                y = torch.rot90(y, k=1, dims=[0, 1])
            if np.random.random_sample() > 0.5:  # rotate 180 degrees
                x = torch.rot90(x, k=2, dims=[1, 2])
                y = torch.rot90(y, k=2, dims=[0, 1])
            if np.random.random_sample() > 0.5:  # rotate 270 degrees
                x = torch.rot90(x, k=3, dims=[1, 2])
                y = torch.rot90(y, k=3, dims=[0, 1])

        # standardize 0.70, 0.30
        # if np.random.random_sample() > 0.70:
        #    image = preprocess.standardizeLocalCalcTensor(image, means, stds)
        # else:
        #    image = preprocess.standardizeGlobalCalcTensor(image)

        return x, y

    # -------------------------------------------------------------------------
    # IO methods
    # -------------------------------------------------------------------------
    def list_files(self, dataset_dir: str, files_list: list = []):

        images_dir = os.path.join(dataset_dir, 'images')
        labels_dir = os.path.join(dataset_dir, 'labels')

        for i in os.listdir(images_dir):
            files_list.append(
                {
                    'image': os.path.join(images_dir, i),
                    'label': os.path.join(labels_dir, i)
                }
            )
        return files_list

    def open_image(self, idx: int, invert: bool = True, norm: bool = True):
        image = imread(self.files[idx]['image'])
        image = image.transpose((2, 0, 1)) if invert else image
        return (image / np.iinfo(image.dtype).max) if norm else image

    def open_mask(self, idx: int, add_dims: bool = False):
        mask = imread(self.files[idx]['label'])
        return np.expand_dims(mask, 0) if add_dims else mask


class Preprocess(Config):

    # -------------------------------------------------------------------------
    # Common methods
    # -------------------------------------------------------------------------
    def preprocess(self):
        """
        Preprocessing function.
        """
        logging.info('Starting Preprocessing Step...')
        # iterate over each file and generate dataset
        return list(map(self._preprocess, self.data_df.index))

    # -------------------------------------------------------------------------
    # Preprocess methods - Modify
    # -------------------------------------------------------------------------
    def modify_bands(
            self, img: xr.core.dataarray.DataArray, drop_bands: list = []):
        """
        Drop multiple bands to existing rasterio object
        """
        # Do not modify if image has the same number of output bands
        if img.shape[0] == len(self.output_bands):
            return img

        # Drop any bands from input that should not be on output
        for ind_id in list(set(self.input_bands) - set(self.output_bands)):
            drop_bands.append(self.input_bands.index(ind_id)+1)
        img = img.drop(dim="band", labels=drop_bands, drop=True)
        return img

    def modify_roi(
            self, img: np.ndarray, mask: np.ndarray,
            ymin: int, ymax: int, xmin: int, xmax: int):
        """
        Crop ROI, from outside to inside based on pixel address
        """
        return img[ymin:ymax, xmin:xmax], mask[ymin:ymax, xmin:xmax]

    def modify_pixel_extremity(
            self, img: np.ndarray, xmin: int = 0, xmax: int = 10000):
        """
        Crop ROI, from outside to inside based on pixel address
        """
        return np.clip(img, xmin, xmax)

    def modify_label_classes(self, mask: np.ndarray, expressions: str):
        """
        Change pixel label values based on expression
        """
        for exp in expressions:
            [(k, v)] = exp.items()
            mask[eval(k, {k.split(' ')[0]: mask})] = v
        return mask

    # -------------------------------------------------------------------------
    # IO methods - Modify
    # -------------------------------------------------------------------------
    def gen_random_tiles(
            self, img: np.ndarray, mask: np.ndarray, n_tiles: int = 100):
        """
        Extract small patches for final dataset
        Args:
            img (numpy array - c, y, x): imagery data
            tile_size (tuple): 2D dimensions of tile
            random_state (int): seed for reproducibility (match image and mask)
            n_patches (int): number of tiles to extract
        """
        tile_size = ((self.tile_size, ) * 2)
        img = image.extract_patches_2d(
            image=img, max_patches=n_tiles,
            patch_size=tile_size, random_state=self.seed)
        mask = image.extract_patches_2d(
            image=mask, max_patches=n_tiles,
            patch_size=tile_size, random_state=self.seed)
        return img, mask

    def gen_random_tiles_include(self):
        raise NotImplementedError

    # -------------------------------------------------------------------------
    # Core methods
    # -------------------------------------------------------------------------
    def _preprocess(self, index: int):

        logging.info(f'File #{index+1}: ' + self.data_df['data'][index])

        # Get filename for output purposes
        filename = self.data_df['data'][index].split('/')[-1]

        # Read imagery from disk and process both image and mask
        img = xr.open_rasterio(
            self.data_df['data'][index], chunks=self.chunks).load()
        mask = xr.open_rasterio(
            self.data_df['label'][index], chunks=self.chunks).values

        logging.info(
            f"File #{index+1}: {filename}, img:{img.shape} label:{mask.shape}")

        # lets modify bands if necessary - in a future version, add indices
        img = self.modify_bands(img)

        # move from chw to hwc, squeze mask if required
        img = np.moveaxis(img.values, 0, -1).astype(np.int16)
        mask = np.squeeze(mask) if len(mask.shape) != 2 else mask
        mask = mask - 1 if np.min(mask) == 1 else mask

        # temporary
        mask[mask == 14] = 5

        # modify labels if needed
        logging.info(f"Unique label classes: {np.unique(mask)}")
        # if self.modify_labels:
        #    mask = self.modify_label_classes(
        #        mask, expressions=self.modify_labels)
        # logging.info(f"Unique label classes after modify: {np.unique(mask)}")

        # modify imagery boundaries
        img = self.modify_pixel_extremity(
            img, xmin=self.data_min, xmax=self.data_max)

        # Get region of interest for training - TODO: automated retrieval
        img, mask = self.modify_roi(
            img, mask,
            ymin=self.data_df['ymin'][index], ymax=self.data_df['ymax'][index],
            xmin=self.data_df['xmin'][index], xmax=self.data_df['xmax'][index])
        logging.info(f"Post preprocessing: {img.shape}, label: {mask.shape}")

        # generate tiles arrays
        img_tiles, mask_tiles = self.gen_random_tiles(
            img, mask, n_tiles=self.data_df['ntiles'][index])
        logging.info(f"After tiling: {img_tiles.shape}, {mask_tiles.shape}")

        # save to disk
        for id in range(img_tiles.shape[0]):
            imsave(
                os.path.join(self.images_dir, f'{filename[:-4]}_{id}.tif'),
                img_tiles[id, :, :, :], planarconfig='contig')
            imsave(
                os.path.join(self.labels_dir, f'{filename[:-4]}_{id}.tif'),
                mask_tiles[id, :, :], planarconfig='contig')
        return index


class Train(Config):

    # -------------------------------------------------------------------------
    # Common methods
    # -------------------------------------------------------------------------
    def train(self):
        """
        Training function.
        """
        logging.info('Starting Training Step...')
        # iterate over each file and generate dataset
        self._train()

    def _train(self):

        # Load model
        self.model = UNet(
            n_channels=len(self.output_bands), n_classes=self.n_classes
        ).to(self.device)

        # enable multi-gpu training
        self.model = nn.DataParallel(self.model, device_ids=[0, 1, 2, 3])
        logging.info("Loaded model...")

        # Read Dataset
        data = CloudDataset(dataset_dir=self.dataset_dir)
        n_train, n_val = len(data), int(len(data) * self.test_size)
        logging.info(f'Generated dataset of size: {n_train}')

        # Generate Datasets
        train_ds, val_ds = torch.utils.data.random_split(
            data, (n_train - n_val, n_val))
        self.train_dl = DataLoader(
            train_ds, batch_size=self.batch_size, shuffle=True)
        self.val_dl = DataLoader(
            val_ds, batch_size=self.batch_size, shuffle=True)

        # Loss and Optimizer
        # self.criterion = nn.CrossEntropyLoss().to(self.device)
        self.criterion = mIoULoss(n_classes=self.n_classes).to(self.device)
        # self.criterion = FocalLoss().to(self.device)
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=0.0001)
        self.scheduler = torch.optim.lr_scheduler.StepLR(
            self.optimizer, step_size=1, gamma=0.5)
        self.min_loss = torch.tensor(float('inf'))
        # self.acc_fn = self.acc_metric

        # Training loop
        overall_loss = self.train_loop()
        return overall_loss

    def train_loop(self):

        plot_losses = []
        scheduler_counter = 0

        for epoch in range(self.max_epoch):

            # training
            self.model.train()

            loss_list = []
            acc_list = []

            for batch_i, (x, y) in enumerate(self.train_dl):

                pred_mask = self.model(x.to(self.device))
                loss = self.criterion(pred_mask, y.to(self.device))

                self.optimizer.zero_grad()
                loss.backward()
                self.optimizer.step()
                loss_list.append(loss.cpu().detach().numpy())
                acc_list.append(self.acc(y, pred_mask).numpy())

                sys.stdout.write(
                    "\r[Epoch %d/%d] [Batch %d/%d] [Loss: %f (%f)]"
                    % (
                        epoch,
                        self.max_epoch,
                        batch_i,
                        len(self.train_dl),
                        loss.cpu().detach().numpy(),
                        np.mean(loss_list),
                    )
                )

            scheduler_counter += 1

            # testing
            self.model.eval()

            val_loss_list = []
            val_acc_list = []

            for batch_i, (x, y) in enumerate(self.val_dl):
                with torch.no_grad():
                    pred_mask = self.model(x.to(self.device))
                val_loss = self.criterion(pred_mask, y.to(self.device))
                val_loss_list.append(val_loss.cpu().detach().numpy())
                val_acc_list.append(self.acc(y, pred_mask).numpy())

            print(' epoch {} loss: {:.5f} acc: {:.2f} valloss : {:.5f} valacc : {:.2f}'.format(
                epoch, np.mean(loss_list), np.mean(acc_list),
                np.mean(val_loss_list), np.mean(val_acc_list)))
            plot_losses.append(
                [epoch, np.mean(loss_list), np.mean(val_loss_list)])

            compare_loss = np.mean(val_loss_list)
            is_best = compare_loss < self.min_loss

            if is_best:
                scheduler_counter = 0
                self.min_loss = min(compare_loss, self.min_loss)
                torch.save(
                    self.model.state_dict(),
                    os.path.join(
                        self.model_dir,
                        'unet_epoch-{}_{}_{:.5f}.pt'.format(
                            self.experiment_name, epoch, np.mean(val_loss_list)
                        )
                    )
                )

            if scheduler_counter > 5:
                self.scheduler.step()
                print(f"Lowering LR to {self.optimizer.param_groups[0]['lr']}")
                scheduler_counter = 0

        return plot_losses

    def acc(self, y, pred):
        return (y.cpu() == torch.argmax(pred, axis=1).cpu()).sum() \
            / torch.numel(y.cpu())


class Predict(Preprocess):

    # -------------------------------------------------------------------------
    # Common methods
    # -------------------------------------------------------------------------
    def predict(self):
        """
        Prediction function.
        """
        logging.info('Starting Prediction Step...')

        # iterate over each file and generate dataset
        try:
            self.model_filename
        except AttributeError:
            models_list = glob.glob(os.path.join(self.model_dir, '*.pt'))
            self.model_filename = max(models_list, key=os.path.getctime)
        logging.info(f'Loading {self.model_filename}')

        # Load model
        self.model = UNet(
            n_channels=len(self.output_bands), n_classes=self.n_classes
        ).to(self.device)
        self.model = nn.DataParallel(self.model, device_ids=[0, 1, 2, 3])
        logging.info("Loaded model...")

        self.model.load_state_dict(torch.load(self.model_filename))
        self.model.eval()

        # get data for prediction
        self.data_predict = glob.glob(self.data_predict)
        # /Senegal_LCLUC/VHR/priority-tiles/Aki-tiles-ETZ/M1BS/WV02_20170922_M1BS_10300100719C2D00-toa.tif']
        # self.data_predict = ['/Users/jacaraba/Desktop/CURRENT_PROJECTS/LCLUC_Senegal_Cloud/zach_labels/WV02_20170922_M1BS_10300100719C2D00_Aki_CLIPPED.tif']
        # self.data_predict = ['/att/nobackup/mwooten3/Senegal_LCLUC/VHR/priority-tiles/Aki-tiles-ETZ/M1BS/WV03_20200214_M1BS_1040010057108200-toa.tif']

        # self.data_predict = [
        #    '/att/nobackup/mwooten3/Senegal_LCLUC/VHR/priority-tiles/Konrad-tiles/M1BS/WV02_20120210_M1BS_1030010011053600-toa.tif',
        #    '/att/nobackup/mwooten3/Senegal_LCLUC/VHR/priority-tiles/Konrad-tiles/M1BS/WV02_20120303_M1BS_1030010012B47700-toa.tif',
        #    #'/att/nobackup/mwooten3/Senegal_LCLUC/VHR/priority-tiles/kassassa_M1BS-8band/WV02_20120210_M1BS_1030010011053600-toa.tif'
        # ]
        logging.info(f'{len(self.data_predict)} files to predict.')

        # iterate over each file and predict
        for r in self.data_predict:
            self._predict(r)

    def _predict(self, filename):

        logging.info(f'File: {filename}')

        # Get filename for output purposes
        raster_name = os.path.join(
            self.inference_output_dir,
            filename[:-4].split('/')[-1] + '_pred.tif')

        # --------------------------------------------------------------------------------
        # if prediction is not on directory, start predicting
        # (allows for restarting script if it was interrupted at some point)
        # --------------------------------------------------------------------------------
        if not os.path.isfile(raster_name):

            img = xr.open_rasterio(filename, chunks=self.chunks).load()

            #### SOMETHING HERE IS SLOW

            # lets modify bands if necessary - in a future version, add indices
            img = self.modify_bands(img)

            # move from chw to hwc, squeze mask if required
            img = np.moveaxis(img.values, 0, -1).astype(np.int16)

            # preprocess here - normalization
            img = (img / np.iinfo(img.dtype).max)

            # modify imagery boundaries
            img = self.modify_pixel_extremity(
                img, xmin=self.data_min, xmax=self.data_max)

            #### SOMETHING HERE IS SLOW

            logging.info(f'Tensor shape: {img.shape}')

            tiler = ImageSlicer(
                img.shape, tile_size=(self.tile_size, self.tile_size),
                tile_step=(self.overlap, self.overlap)
            )

            # Get tiles to predict
            tiles = list()
            for tile in tiler.split(img):
                image = np.moveaxis(tile, -1, 0)
                image = np.ascontiguousarray(image)
                tiles.append(torch.from_numpy(image).float())

            # Allocate a CUDA buffer for holding entire mask
            merger = TileMerger(tiler.target_shape, 1, tiler.weight)

            # Run predictions for tiles and accumulate them
            dataloader = DataLoader(
                list(zip(tiles, tiler.crops)), batch_size=self.pred_batch_size,
                pin_memory=True
            )

            for tiles_batch, coords_batch in dataloader:
                tiles_batch = tiles_batch.float().to(self.device)
                pred_batch = self.model(tiles_batch)
                pred_batch = torch.argmax(
                    torch.nn.functional.softmax(pred_batch, dim=1), dim=1)
                merger.integrate_batch(pred_batch, coords_batch)

            # Normalize accumulated mask and convert back to numpy
            merged_mask = np.moveaxis(
                to_numpy(merger.merge()), 0, -1).astype(np.uint8)
            merged_mask = tiler.crop_to_orignal_size(merged_mask)
            merged_mask = np.squeeze(merged_mask, axis=-1)

            # post-processing
            # kernel = np.ones((64, 64), np.uint8)
            # merged_mask = cv2.morphologyEx(merged_mask, cv2.MORPH_OPEN, kernel)  # opening
            # merged_mask = cv2.morphologyEx(merged_mask, cv2.MORPH_CLOSE, kernel)  # closing
            # merged_mask = cv2.dilate(merged_mask, kernel, iterations = 1)  # dilation
            # merged_mask = cv2.medianBlur(merged_mask, 25)
            merged_mask = median_filter(merged_mask, size=25)
            #merged_mask = self._binary_fill(merged_mask)

            # dilation = cv2.dilate(img,kernel,iterations = 1)
            # merged_mask = self._grow(merged_mask)
            # merged_mask = self._denoise(merged_mask)
            # merged_mask = self._binary_fill(merged_mask)

            self.arr_to_tif(filename, merged_mask, raster_name, ndval=-9999)
            logging.info(f'Saved Filename: {raster_name}')

        # This is the case where the prediction was already saved
        else:
            logging.info(f'{raster_name} already predicted.')

        return
 
    def _grow(self, merged_mask, eps=120):
        struct = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (eps, eps))
        return cv2.morphologyEx(merged_mask, cv2.MORPH_CLOSE, struct)

    def _denoise(self, merged_mask, eps=30):
        struct = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (eps, eps))
        return cv2.morphologyEx(merged_mask, cv2.MORPH_OPEN, struct)

    def _binary_fill(self, merged_mask):
        return binary_fill_holes(merged_mask).astype(int)

    def arr_to_tif(self, raster_f, segments, out_tif='s.tif', ndval=-9999):
        """
        Save array into GeoTIF file.
        Args:
            raster_f (str): input data filename
            segments (numpy.array): array with values
            out_tif (str): output filename
            ndval (int): no data value
        Return:
            save GeoTif to local disk
        ----------
        Example
        ----------
            arr_to_tif('inp.tif', segments, 'out.tif', ndval=-9999)
        """
        # get geospatial profile, will apply for output file
        with rio.open(raster_f) as src:
            meta = src.profile
            nodatavals = src.read_masks(1).astype('int16')
        # print(meta)

        # load numpy array if file is given
        if type(segments) == str:
            segments = np.load(segments)
        segments = segments.astype('int16')
        # print(segments.dtype)  # check datatype

        nodatavals[nodatavals == 0] = ndval
        segments[nodatavals == ndval] = nodatavals[nodatavals == ndval]

        out_meta = meta  # modify profile based on numpy array
        out_meta['count'] = 1  # output is single band
        out_meta['dtype'] = 'int16'  # data type is float64

        # write to a raster
        with rio.open(out_tif, 'w', **out_meta) as dst:
            dst.write(segments, 1)


class Pipeline(Train, Predict):

    def __init__(self, yaml_filename: str, csv_filename: str):

        Preprocess.__init__(self, yaml_filename, csv_filename)
