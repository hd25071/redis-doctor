---
id: hb-storage-provision
title: PVC 一直 Pending（存储供给失败）
category: storage_provision
source: Kubernetes 官方排障文档 - PersistentVolume / StorageClass
symptom: PVC 长时间不是 Bound，Pod 卡在 Pending
---

## 现象

- `kubectl get pvc -n demo` 显示 STATUS=Pending。
- `kubectl describe pvc` 的事件里出现 `ProvisioningFailed`，消息里带 `storageclass ... not found`。
- Pod 的 `FailedScheduling` 事件里出现 `unbound immediate PersistentVolumeClaims`。

## 排查步骤

1. `kubectl get pvc -n demo`：确认哪个 PVC 没有 Bound。
2. `kubectl describe pvc <pvc>`：读 ProvisioningFailed / FailedBinding 的事件消息。
3. `kubectl get storageclass`：确认 PVC 引用的 storageClass 是否存在、是否带 `(default)` 标记。
4. `kubectl get pod <pod> -o yaml`：确认 Pod 是因为卷未就绪而无法调度，而不是资源不足。

## 常见原因

- storageClassName 拼错或该 StorageClass 已被删除。
- 集群没有默认 StorageClass，PVC 又没显式指定。
- 动态供给的 provisioner 不可用（local-path provisioner 挂掉）。

## 处置

- 修正 CR 里的 storageSize 或 storageClass，删除未 Bound 的 PVC 让 StatefulSet 重建（属于 L2，只给建议命令）。

## 坑

- StatefulSet 的 volumeClaimTemplates 改动**不会**自动应用到已存在的 PVC：必须删除旧 PVC 才能生效，操作前先确认数据是否可以丢。

