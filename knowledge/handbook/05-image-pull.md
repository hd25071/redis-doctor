---
id: hb-image-pull
title: 镜像拉取失败导致 Pod 起不来
category: image_pull
source: Kubernetes 官方排障文档 - Images
symptom: Pod 处于 ImagePullBackOff / ErrImagePull
---

## 现象

- Pod 状态是 `Pending`，`kubectl get pods` 显示 `ImagePullBackOff`。
- 事件里出现 `Failed to pull image ... not found` 或 `unauthorized`。

## 排查步骤

1. `kubectl describe pod <pod>`：读 Waiting Reason 与 Failed 事件里完整的镜像名。
2. 确认镜像标签是否真实存在（版本号写错是最常见的原因，例如 `7.2.9-typo`）。
3. 私有仓库要检查 imagePullSecrets 是否存在且有效。
4. 在节点上直接 `crictl pull <image>` 可以区分"仓库问题"和"集群凭证问题"。

## 常见原因

- CR 里 `spec.version` 写了一个不存在的标签。
- 镜像仓库网络不通（国内环境需要 registries.yaml 镜像加速）。
- 私有仓库凭证过期。

## 处置

- 把 `spec.version` 改回存在的标签（L2，给建议命令），旧 Pod 会随滚动更新被替换。

## 坑

- 只看到 `Pending` 就说"资源不足"是错的：一定要把事件消息读完，调度失败和拉取失败是两条完全不同的路径。

