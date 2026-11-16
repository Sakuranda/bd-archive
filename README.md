# BD Archive

在本机整理 Blu-ray 备份目录。查看已刻录张数和总容量，按文件名或文件修改日期搜索，再定位到对应光盘与盘内路径。目录、备注、文件夹视图和换标记历史储存在 SQLite 中。

## 启动

安装 Python 3.10 或以上版本。在 Windows 上双击 `start.bat`，或者从项目目录运行下列命令。默认地址是 `http://127.0.0.1:8619/`，关闭启动窗口即可停止服务。

```powershell
python archive.py --open
```

可用 `--port 8620` 指定端口，用 `--db <数据库路径>` 指定 SQLite 文件。首次启动会创建 `data/catalog.sqlite3`。

## 导入光盘目录

导入器按盘号读取备份源中的文件和子目录。默认从 `BD已刻录` 目录读入已刻录光盘，从备份源的直接子目录读入待刻录光盘。盘号目录可写成 `BD-1` 或 `BD-01`。可以指定待刻录的盘号区间，并将多张光盘的目录叠放到同一个逻辑文件夹中。

```powershell
python import_catalog.py "C:\Archive\Input" --pending-start 8 --pending-end 14 --group "照片归档" 1 3
```

`--burned-folder` 可指定已刻录目录名，`--database` 可指定数据库文件。再次扫描时可以沿用数据库保存的导入设置，已添加的备注和整理后的逻辑目录会一并保留。`python import_catalog.py --help` 会列出详细选项。

## 查找和整理

- **主页**显示已刻录容量与张数，可按文件名或修改日期查找。结果显示对应盘号和路径。
- **光盘内容详情**提供可折叠的文件树。可以给盘号和树中的条目添加备注。
- **文件夹视图**用于按主题整理逻辑文件夹和光盘引用。可以新建文件夹、写备注，并复制、剪切、粘贴文件夹或光盘引用。操作保存在目录数据库中。
- **补标记光盘**把旧盘号下的目录映射到新的盘号。提交前核对两个盘号，提交后可在历史记录查看变更。

## 备份数据库

使用 SQLite 快照命令可在服务运行时生成一致的备份。`--check` 会检查数据库完整性。

```powershell
python archive.py --backup "C:\Archive\catalog-backup.sqlite3"
python archive.py --db "C:\Archive\catalog-backup.sqlite3" --check
```

需要恢复时，可以通过 `--db` 指向快照，或停止服务后把快照复制为 `data/catalog.sqlite3`。文件名、目录路径、备注和换标记历史都在数据库里，请把数据库保存在自己的设备或私有存储中。
