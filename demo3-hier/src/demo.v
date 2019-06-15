module top (
  input clk,
  input [7:0] addr,
  input [7:0] wdata,
  output [7:0] rdata,
);
  const rand reg [7:0] test_addr;
  reg [7:0] test_data;
  reg test_valid = 0;

  always @(posedge clk) begin
    if (addr == test_addr) begin
      if (test_valid)
        assert(test_data == rdata);
      test_data <= wdata;
      test_valid <= 1;
    end
  end

  memory uut (
    .clk  (clk  ),
    .addr (addr ),
    .wdata(wdata),
    .rdata(rdata)
  );
endmodule


module memory (
  input clk,
  input [7:0] addr,
  input [7:0] wdata,
  output [7:0] rdata,
);
  reg [7:0] mem [0:255];

  always @(posedge clk)
    mem[addr] <= wdata;

  assign rdata = mem[addr];
endmodule
