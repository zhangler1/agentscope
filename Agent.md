==> 行内自定义whl依赖打包
1. 必须保持此结构
2. 我们是基于agentscope==2.0.5的源码，增加了行内的适配（src）中
3. 为了方便了解源码和修改，我将agentscope==2.0.5源码下载到src/agentscope,但是这并不会包含在最终提交
4. bocom-starter是为了验证bocom-starter是否可用的程序，也不会包含最终提交
5. 开发手册写到bocom-starter中
6. 生成的whl文件放到bocom-starter/wheels中
7. gradle.properties,settings.gradle,setup.py是行内自研依赖打包用的