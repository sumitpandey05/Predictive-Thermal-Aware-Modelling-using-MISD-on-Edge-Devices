obj-m += misd_gov.o

# Replace this path with your actual Raspberry Pi Linux source tree path in WSL2
KDIR := $(HOME)/projects/misd_edge_ai/rpi-kernel

# Cross-compilation variables for Cortex-A72 (ARM64)
ARCH ?= arm64
CROSS_COMPILE ?= aarch64-linux-gnu-

all:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) modules

clean:
	$(MAKE) -C $(KDIR) M=$(PWD) ARCH=$(ARCH) CROSS_COMPILE=$(CROSS_COMPILE) clean