#!/usr/bin/python3
import shutil
import os
filetype = {
    'video': ['.mp4', '.mkv', '.3gp', '.avi', '.vob', '.m2ts', '.mov', '.mpg4'],
    'music': ['.mp3', '.3ga', '.aifc', '.m3u', '.m3u8', '.m4p', '.m4r', '.opus'],
    'image': ['.jpg', '.png', '.xcf', '.jpeg', '.gif', '.tif', '.webp'],
    'docs': ['.json', '.pdf', '.txt', '.doc', '.html', '.htm', '.xls', '.xlsx', '.ppt', '.pptx', '.docx', '.csv', '.dat', '.db', '.dbf', '.log', '.mdb', '.sav', '.sql', '.xml', '.epub'],
    'compress': ['.gz', '.xz', '.bz', '.zip', '.rar', '.7z', '.arj', '.txz'],
    'disk': ['.img', '.iso', '.toast', '.vcd'],
    'programs': ['.exe', '.AppImage', '.deb', '.rpm', '.dmg', '.bin', '.jar', '.py'],
    'torrent': ['.torrent'],
    'fonts': ['.otf', '.tff']
}


path = '/home/arnab/Downloads'
files = os.listdir(path)


def organize(file_type, file_folder):
    for file in files:
        temp = file.split('.')
        ext = '.' + temp[-1]
        for i in filetype[file_type]:
            if i == ext:
                if os.path.isdir(os.path.join(path, file_folder)):
                    shutil.move(os.path.join(path, file),
                                os.path.join(path, file_folder))
                else:
                    os.mkdir(os.path.join(path, file_folder))
                    shutil.move(os.path.join(path, file),
                                os.path.join(path, file_folder))


organize('video', 'Video')
organize('music', 'Music')
organize('image', 'Images')
organize('docs', 'Documents')
organize('compress', 'Compressed')
organize('disk', 'Disk Images')
organize('torrent', 'Torrents')
organize('fonts', 'Fonts')
organize('programs', 'Programs')
