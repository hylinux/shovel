cd C:\\Users\\honguo\\source\\repos\\Python\\shovel



\# 1. 导出进度信息（提交历史、当前状态、未提交的改动）

git log --oneline --graph -100 | Out-File \_gitlog.txt -Encoding utf8

git status | Out-File \_gitstatus.txt -Encoding utf8

git diff | Out-File \_uncommitted.diff -Encoding utf8



\# 2. 列出要打包的文件：已提交 + 新建但未忽略的文件，排除 .env 类密钥文件

$files = git ls-files -co --exclude-standard |

&#x20; Where-Object { (Test-Path -LiteralPath $\_ -PathType Leaf) -and ($\_ -notmatch '(^|/)\\.env') }



$files += '\_gitlog.txt','\_gitstatus.txt','\_uncommitted.diff'



\[IO.File]::WriteAllLines("$PWD\\\_filelist.txt", ($files | Select-Object -Unique))



\# 3. 打成 zip，放到桌面，保留目录结构

tar -a -c -f "$env:USERPROFILE\\shovel\_snapshot.zip" -T \_filelist.txt



\# 4. 删掉临时文件

Remove-Item \_gitlog.txt,\_gitstatus.txt,\_uncommitted.diff,\_filelist.txt

