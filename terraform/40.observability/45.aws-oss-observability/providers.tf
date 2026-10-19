terraform {
  required_version = ">= 1.1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.69.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.22"
    }
    kubectl = {
      source  = "alekc/kubectl"
      version = "2.4.1"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.0"
    }
    time = {
      source  = "hashicorp/time"
      version = "0.14.2"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.0"
    }
  }
}

provider "aws" {
  default_tags {
    tags = local.tags
  }
}

provider "kubernetes" {
  host                   = local.eks_cluster_endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes = {
    host                   = local.eks_cluster_endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}
