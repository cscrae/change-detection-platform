import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
const root='D:/WorkSpace/Research/003_change_detection/Code/change-detection-platform';
const work=root+'/reports/20261017-progress', build=work+'/.build', out=work+'/output';
// Supply local installation paths explicitly; never commit a user's home directory.
const skill=process.env.CDP_PRESENTATIONS_SKILL_DIR?.trim();
const runtime=process.env.CDP_ARTIFACT_RUNTIME_DIR?.trim();
if(!skill||!runtime)throw new Error('Set CDP_PRESENTATIONS_SKILL_DIR and CDP_ARTIFACT_RUNTIME_DIR to the local presentation skill and workspace runtime directories before building this report.');
for(const [name,directory] of [['CDP_PRESENTATIONS_SKILL_DIR',skill],['CDP_ARTIFACT_RUNTIME_DIR',runtime]]){
  try{if(!(await fs.stat(directory)).isDirectory())throw new Error();}
  catch{throw new Error(name+' must point to an existing local directory.');}
}
const {Presentation,PresentationFile}=await import('@oai/artifact-tool');
const C={navy:'#123458',blue:'#1765A0',teal:'#008B92',ink:'#24364A',muted:'#62748A',light:'#EDF3F8',line:'#D7E2EC',amber:'#A46B15'};
const p=Presentation.create({slideSize:{width:1280,height:720}});
function rect(s,x,y,w,h,fill){return s.shapes.add({geometry:'rect',position:{left:x,top:y,width:w,height:h},fill,line:{fill:'none',width:0}});}
function txt(s,t,x,y,w,h,size=25,color=C.ink,bold=false){const a=rect(s,x,y,w,h,'none');a.text=t;a.text.style={typeface:'Microsoft YaHei',fontSize:size,color,bold,...(/^0[1-4]$/.test(t)?{insets:0,wrap:'none'}:{})};return a;}
function slide(title,sub,notes){const s=p.slides.add();s.background.fill='#FFFFFF';rect(s,0,0,1280,10,C.navy);txt(s,title,64,45,1150,62,38,C.navy,true);if(sub)txt(s,sub,66,112,1140,42,20,C.muted);rect(s,64,665,1152,1,C.line);txt(s,'遥感变化检测平台 · 阶段进展',64,680,750,22,14,C.muted);txt(s,String(p.slides.items.length).padStart(2,'0'),1160,677,55,26,17,C.muted);s.speakerNotes.text=notes;return s;}
function lineItem(s,n,title,body,y,x=70,w=1120){txt(s,n,x,y,65,55,34,C.teal,true);txt(s,title,x+83,y,w-83,40,27,C.navy,true);txt(s,body,x+83,y+48,w-83,72,23,C.muted);}
async function pic(s,file,x,y,w,h){const b=await fs.readFile(root+'/data/validation/'+file);s.images.add({blob:b.buffer.slice(b.byteOffset,b.byteOffset+b.byteLength),contentType:'image/png',fit:'contain',position:{left:x,top:y,width:w,height:h},alt:'平台功能演示截图，测试夹具，不代表真实检测效果'});}
const src=(t)=>'\n\n证据（项目根目录相对路径）：'+t+'\n核对日期：2026-10-17。报告为已有记录，本次制作未重跑产品验收。';
let s=slide('','', '建议用时 30 秒。介绍平台面向河北区域的本地遥感影像变化检测，本次汇报覆盖截至当前的阶段成果。以当前工作区为准，含尚未提交的行政区改进；不能等同于已发布版本。'+src('README.md；docs/CURRENT_STATUS.md；docs/administrative-boundaries.md'));
txt(s,'遥感变化检测平台',70,170,1120,95,58,C.navy,true);txt(s,'阶段进展汇报',74,285,1000,70,40,C.blue);rect(s,76,393,93,5,C.teal);txt(s,'影像管理  /  检测执行  /  结果分析  /  AI 辅助',74,438,1120,55,27,C.muted);txt(s,'2026 年 10 月 17 日',74,572,900,40,23,C.muted);
s=slide('阶段进展概览','已形成平台主要业务链路，下一步重点补齐业务效果与持续运行验证', '建议用时 60 秒。按四类成果说明阶段进展，不使用完成百分比。已有软件能力不等于业务精度已验收，AI 仍以受控工具调用为边界。'+src('README.md；docs/CURRENT_STATUS.md；docs/ai-assistant-review.md'));
const overview=[['01','影像管理','本地影像索引与月时相组织\n行政区导航、完整县界选区'],['02','检测执行','选区与时相提交、队列进度\n模型及参数随批次冻结'],['03','结果分析','前后时相与变化结果联动\n图斑核查、统计与成果导出'],['04','AI 辅助','自然语言调用平台操作\n复用手动检测提交入口']];
overview.forEach((a,i)=>{let x=70+(i%2)*595,y=190+Math.floor(i/2)*215;txt(s,a[0],x,y,65,60,42,C.teal,true);txt(s,a[1],x+82,y+3,450,45,30,C.navy,true);txt(s,a[2],x+82,y+67,460,100,25);});
s=slide('建设目标与业务流程','把影像、模型和结果组织到同一工作区，支持变化线索发现与人工核查', '建议用时 60 秒。按流程讲业务用途。平台产出变化候选，仍需人员结合标注或业务资料核查。月时相由同月有效影像组织；来源覆盖必须结合实际目录判断。'+src('README.md；docs/CURRENT_STATUS.md；frontend/src/composables/useWorkspace.ts'));
['影像接入','区域与时相','变化检测','核查与导出'].forEach((a,i)=>{let x=64+i*299;rect(s,x,230,255,94,i===2?C.navy:C.light);txt(s,a,x+18,257,222,50,29,i===2?'#FFFFFF':C.navy,true);if(i<3)txt(s,'→',x+263,253,40,50,32,C.teal);});
['登记目录\n建立月时相索引','选择行政区或手绘选区\n明确比较时相','模型推理与后处理\n查看任务状态','联动查看变化图斑\n导出空间与统计成果'].forEach((a,i)=>txt(s,a,68+i*299,356,265,112,24));
rect(s,64,535,1152,73,C.light);txt(s,'业务定位：辅助发现变化线索，为人工核查提供空间位置与结果依据',84,553,1110,45,26,C.navy,true);
s=slide('平台整体架构','前端交互、任务管理、影像处理与 GPU 推理分工明确', '建议用时 60 秒。强调分工与可追溯性，不逐项讲技术栈细节。平台检测使用 CUDA ONNX，独立 CLI 的 CPU 能力不作为产品能力。源影像和模型只读，派生结果单独保存。'+src('README.md；docs/CURRENT_STATUS.md；backend-go/internal/api/images.go；frontend/src/composables/useWorkspace.ts'));
const layers=[['交互层','概览  ·  对比  ·  报告  ·  管理  ·  AI 助手','Vue / TypeScript'],['业务层','配置版本  ·  任务队列  ·  状态与结果记录','Go / SQLite'],['计算层','影像索引、裁切与后处理  →  ONNX GPU 推理','Python / CUDA']];
layers.forEach((a,i)=>{let y=190+i*125;rect(s,65,y,1150,94,i===1?C.navy:C.light);txt(s,a[0],87,y+25,150,47,29,i===1?'#FFFFFF':C.navy,true);txt(s,a[1],258,y+29,740,43,25,i===1?'#FFFFFF':C.ink);txt(s,a[2],1003,y+35,200,35,17,i===1?'#D4E4F1':C.muted);});txt(s,'数据边界：原始影像与模型保留，裁切、任务快照和结果单独管理',78,590,1140,45,24,C.muted);
s=slide('影像与行政区管理','已将影像来源入口与行政区层级分开组织', '建议用时 75 秒。14 个数指来源入口，不代表全省影像全覆盖。行政知识库包含 1 省、11 市、167 县。完整县界保留部件和孔洞，服务端校验代码、来源及版本。截图为真实边界配合模拟影像任务接口，仅展示操作能力，未完成真实整县推理。'+src('docs/administrative-boundaries.md；data/validation/administrative-source-14/report.json；data/validation/administrative-boundaries/validation-report.md；frontend/src/composables/useWorkspace.ts'));
await pic(s,'administrative-source-14/desktop-county.png',62,178,728,450);txt(s,'14 个影像来源入口',830,190,390,50,30,C.navy,true);txt(s,'行政区知识库\n11 市 · 167 县',830,263,390,90,29,C.teal,true);txt(s,'按月组织可用时相\n显式选择完整县界\n保留多部件与孔洞',830,382,390,140,25);txt(s,'入口数量 ≠ 完整影像覆盖',830,566,390,48,22,C.amber);txt(s,'功能演示：真实边界 + 模拟影像与任务',66,633,740,25,16,C.muted);
s=slide('检测任务流程','选区、时相、模型与参数共同决定一次检测任务', '建议用时 65 秒。自定义选区为矩形及单环多边形；可信县界是独立完整几何路径。多时相默认相邻组顺序提交，AI 明确两个日期时仅选对应两期。切页可继续，刷新会丢失尚未提交组的前端编排。'+src('frontend/src/composables/useWorkspace.ts；docs/CURRENT_STATUS.md；docs/ai-assistant-review.md；docs/administrative-boundaries.md'));
lineItem(s,'01','确定检测条件','选择矩形、多边形或完整县界；确认比较时相与模型参数',180);
lineItem(s,'02','提交并跟踪任务','裁切准备与检测进度分阶段展示；多时相按相邻组推进',330);
lineItem(s,'03','保留运行依据','冻结模型、配置版本和参数，使同批次任务使用一致条件',480);
s=slide('结果展示与应用','联动对比与图斑统计支持定位变化、逐项核查和成果交付', '建议用时 75 秒。截图来自确定性浏览器夹具，图像、面积、图斑数和置信度均不作为实测业务成果。当前功能支持三图联动、热点和图斑检查。置信度分布用于查看模型得分，不等于准确率。'+src('data/validation/workspace-ui/report.json；docs/CURRENT_STATUS.md；frontend/src/views/Comparison.vue；frontend/src/views/Reports.vue'));
await pic(s,'workspace-ui/comparison-1920.png',64,180,800,450);txt(s,'三图联动',900,200,300,45,30,C.navy,true);txt(s,'前时相 / 后时相 / 变化结果',900,258,300,90,23);txt(s,'图斑与统计',900,360,300,45,30,C.navy,true);txt(s,'TIFF · GeoJSON\nCSV · Shapefile ZIP',900,420,310,100,24);txt(s,'功能演示：测试数据，不代表真实检测效果；置信度不等于准确率',66,633,1150,25,16,C.amber);
s=slide('AI 助手近期进展','自然语言操作复用平台业务入口，避免绕过现有检测校验', '建议用时 80 秒。示例是表达方式示意，不是本次真实云模型对话记录。关键改进是完整目录可查询、明确两期不扩大为日期区间、矩形 AOI 可提交、版本变化和重复提交受控。测试采用模型和 HTTP 夹具，不能宣称真实语义识别成功率或效率提升。'+src('docs/ai-assistant-review.md；data/validation/ai-browser/report.json；frontend/src/composables/useWorkspace.ts；backend-go/internal/api/ai_domain_services.go'));
rect(s,64,186,1152,96,C.light);txt(s,'“沿用当前选区，对比 2025 年 1 月与 5 月并提交检测。”',86,212,1110,52,28,C.navy,true);txt(s,'指令示例，非本次真实云端对话记录',81,298,1120,29,18,C.muted);
['识别区域与时相','核对工作区状态','调用统一提交入口'].forEach((a,i)=>{let x=70+395*i;txt(s,String(i+1).padStart(2,'0'),x,375,65,55,38,C.teal,true);txt(s,a,x,445,370,45,28,C.navy,true);if(i<2)txt(s,'→',x+325,379,50,45,33,C.teal);});
txt(s,'沿用已有选区与未覆盖参数；资源冲突、状态过期或信息不足时给出明确反馈',70,561,1130,70,25);
s=slide('稳定性与验证','功能验证已有证据，真实业务效果与完整运行验收仍需补齐', '建议用时 80 秒。行政区 live-api-report 与 validation-report 支持真实服务入库和查询；浏览器报告是模拟影像与任务。AI 回归使用确定性夹具。历史文档提及 CUDA 推理与导出通过，但 management 报告缺失，polygon/progress 目录读取被拒绝，因此本次未独立核实，不能作为当前版本全面验收结论。'+src('data/validation/administrative-boundaries/live-api-report.json；data/validation/administrative-boundaries/validation-report.md；data/validation/administrative-source-14/report.json；data/validation/ai-browser/report.json；data/validation/workspace-ui/report.json；docs/CURRENT_STATUS.md'));
lineItem(s,'01','已有局部验证｜真实服务','行政区入库与查询、代码和版本一致性已记录；未运行真实整县推理',185);
lineItem(s,'02','已有功能验证｜确定性夹具','县界操作、时相选择、重复提交拦截、异常反馈和结果界面',335);
lineItem(s,'03','仍需复核｜真实运行与效果','历史 CUDA 记录本轮未独立核实；云端 AI、业务精度和长时负载待验证',485);
s=slide('当前不足','下一阶段的重点是证明“结果可用”和“运行可靠”', '建议用时 75 秒。精度与稳定性是两个独立维度。不要将运行通过当成精度可靠，也不要用一次演示替代大范围长时验证。刷新/关闭会丢失未提交组编排，Go 重启会将未完成任务标为中断失败。'+src('docs/CURRENT_STATUS.md；docs/ROADMAP.md；docs/ai-assistant-capability-todo.md'));
const gaps=[['业务精度','缺少代表性标注评估','明确漏检、误检与适用范围'],['大范围运行','整幅处理存在资源压力','测量时间、内存与显存'],['任务恢复','刷新与重启恢复不完整','验证批次恢复和状态一致性'],['真实 AI 调用','现有回归以夹具为主','验证理解、执行与错误恢复']];
gaps.forEach((a,i)=>{let y=188+i*106;txt(s,a[0],70,y,230,45,29,C.navy,true);txt(s,a[1],322,y+1,420,44,25);txt(s,a[2],785,y+1,430,44,25,C.muted);rect(s,70,y+76,1140,1,C.line);});
s=slide('下一阶段工作','建议优先级：真实案例与精度评估 → 持续运行 → AI 能力演进', '建议用时 75 秒。此页为建议优先级，不是已承诺排期。第一步先锁定试点区域、时相和参考标注，再建立可重复案例及精度指标；第二步测大范围运行与恢复；第三步在约束范围内评估 AI 真实调用并推进持续工作流、自主探索。后两者仍为规划。'+src('docs/ROADMAP.md；docs/ai-assistant-capability-todo.md'));
lineItem(s,'01','先建立可信案例','确定试点区域与标注；记录检测条件、核查结果及误检漏检',185);
lineItem(s,'02','再验证持续运行','测试较大范围、长时间运行、断连与重启；据瓶颈安排改进',335);
lineItem(s,'03','分阶段推进 AI｜规划','先验真实调用，再推进持续工作流与有界自主探索',485);
s=slide('总结与讨论','平台主要能力已形成，下一步以真实案例和验收口径收敛工作', '建议用时 60 秒。总结当前已实现的主要流程及近期行政区、AI 进展。请领导讨论试点范围、标注条件与验收目标；这些是本次建议，不是已知阻塞或已确认资源需求。不要承诺未经讨论的日期。'+src('README.md；docs/CURRENT_STATUS.md；docs/ROADMAP.md；docs/ai-assistant-capability-todo.md'));
txt(s,'已形成',70,192,210,50,31,C.teal,true);txt(s,'影像组织、检测提交、结果核查与导出\n行政区完整选区与 AI 受控操作能力',300,190,900,105,31,C.navy,true);rect(s,70,339,1140,2,C.line);txt(s,'建议讨论',70,380,220,50,31,C.teal,true);txt(s,'试点区域与对比时相\n参考标注及人工核查方式\n精度、运行规模与恢复能力的验收口径',300,379,890,180,30,C.navy);txt(s,'先用代表性案例验证可用性，再确定扩大应用与能力演进节奏',70,599,1140,40,24,C.muted);
await fs.writeFile(build+'/deck.json',JSON.stringify(p.toProto()));
await (await PresentationFile.exportPptx(p)).save(build+'/candidate.pptx');
for(let i=0;i<p.slides.items.length;i++){const b=await p.export({slide:p.slides.items[i],format:'png',scale:1});await fs.writeFile(build+`/slide-${String(i+1).padStart(2,'0')}.png`,new Uint8Array(await b.arrayBuffer()));}
const {finalizePresentation}=await import(pathToFileURL(skill+'/container_tools/artifact_tool_utils.mjs').href);
const r=await finalizePresentation({workspaceDir:work,candidatePath:build+'/candidate.pptx',finalPath:out+'/遥感变化检测平台_组会汇报_20261017.pptx',pythonExecutable:runtime+'/python/python.exe',integrityValidatorPath:skill+'/container_tools/inspect_presentation_package_integrity.py',layoutValidatorPath:skill+'/container_tools/inspect_presentation_layout_geometry.py',layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit'],explicitTotalSlideCount:12,requiredNativeTableOwnerSlides:[],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:['Microsoft YaHei']},verifyArtifactToolImport:true,receiptPath:build+'/validation-final.json'});console.log(JSON.stringify(r));



