# 可选 PP-StructureV3 运行组件（3.2.5）

普通 OpenThesis ZIP 不附带 Paddle 运行环境或模型权重。用户明确同意后才隔离安装；不训练模型、不上传财报。安装与启用是两步独立操作。

代码版本：PaddleOCR 3.7.0 / PaddleX 3.7.2 / PaddlePaddle 3.2.0。代码项目声明 Apache-2.0；安装依赖各自的许可证仍适用。金额、单位、期间与 scope 始终由 OpenThesis 的统一财务验证复核。

下列已选权重的官方模型卡均声明 Apache-2.0。锁清单另行固定 30 个推理文件的 SHA-256 与字节数，总计 957,714,145 bytes；不将代码许可推定为所有未知模型的许可。

| 模型 | 官方许可/模型卡（固定 revision） |
|---|---|
| PP-DocLayout_plus-L | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-DocLayout_plus-L/blob/aa52b8528c84f9b1a34ac3a88fe0e576edb9d11d/README.md) |
| PP-LCNet_x1_0_doc_ori | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori/blob/d3b95a6dff5fe8a94f2748e12b61cb26818a0df8/README.md) |
| PP-LCNet_x1_0_table_cls | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_table_cls/blob/2fa6323e7dab88fa883081db1460995f46af2922/README.md) |
| PP-LCNet_x1_0_textline_ori | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_textline_ori/blob/cd237a44b0e359d4fe38310a416203cf7403faa5/README.md) |
| PP-OCRv5_server_det | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_det/blob/ca867c897ecbca8873081573a802ad70d499cb94/README.md) |
| PP-OCRv5_server_rec | [Apache-2.0](https://huggingface.co/PaddlePaddle/PP-OCRv5_server_rec/blob/b26c3587fda8da3c8ec0ce357214b4d661ff1558/README.md) |
| RT-DETR-L_wired_table_cell_det | [Apache-2.0](https://huggingface.co/PaddlePaddle/RT-DETR-L_wired_table_cell_det/blob/e2bd53c06b3a815d86acbf5c6779dada58819cfe/README.md) |
| RT-DETR-L_wireless_table_cell_det | [Apache-2.0](https://huggingface.co/PaddlePaddle/RT-DETR-L_wireless_table_cell_det/blob/25ca86356a601c877476bb0dcc5fd09153d9d64d/README.md) |
| SLANet_plus | [Apache-2.0](https://huggingface.co/PaddlePaddle/SLANet_plus/blob/bae6e5f8c3c4e7da0c0b7639fdf3228fe76184e2/README.md) |
| SLANeXt_wired | [Apache-2.0](https://huggingface.co/PaddlePaddle/SLANeXt_wired/blob/763069fcda6a065f2171753205a32bf899a88d15/README.md) |


安装从固定官方 revision 获取文件，完整哈希通过后才推理。默认关闭文档纠偏、公式、图表、印章与区域模块，仅加载文档版面、文本检测/识别、表格分类/结构/单元格及必要方向模块。不是加载三套大型本地解析框架。

可选环境要求 Windows x64 / Python 3.12；组件在工作区 Windows CPU 已执行完整推理及安装自检。初次安装可能下载数 GB；准备阶段可取消，取消不删除已验证缓存，也不自动开启云端识别。

模型识别结果是未核验结构候选，不能作为模型正确率、公司覆盖率或财务真值保证。
